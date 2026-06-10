"""
NinjaOne OAuth2 token management.

Credentials are loaded from /app/data/credentials.json, written by the admin
web UI setup flow at /setup.  If the file does not exist the bot starts in an
unconfigured state and directs users to complete setup.

If ENCRYPTION_KEY is set (recommended), credentials.json is encrypted with
Fernet symmetric encryption so a leaked volume backup is unreadable without
the key.  Generate a key with:
    python -c "import secrets; print(secrets.token_hex(32))"

The access token is never stored — get_ninja_token() exchanges the saved
refresh token for a fresh access token automatically whenever one is needed.
Refresh token rotation is persisted back to credentials.json on each cycle.

The shared token cache is a module-level singleton — all importers share the
same access token and benefit from each other's refreshes.
"""

import base64
import json
import logging
import os
import time

import httpx

logger = logging.getLogger("eng_assist_bot.auth")

ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

_CREDS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "credentials.json"
)

# Shared mutable token state
_token: dict = {
    "access_token":  "",
    "expires_at":    0.0,
    "refresh_token": "",
}

# Cached credential dict; None means not yet loaded
_creds: dict | None = None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _creds_path() -> str:
    return os.environ.get("NINJA_CREDS_FILE", _CREDS_FILE)


def data_dir() -> str:
    """Return the directory that holds credentials.json and other runtime files."""
    return os.path.dirname(_creds_path())


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

def _fernet():
    from cryptography.fernet import Fernet
    try:
        key_bytes = bytes.fromhex(ENCRYPTION_KEY)
    except ValueError:
        raise ValueError("ENCRYPTION_KEY contains invalid hex characters")
    if len(key_bytes) != 32:
        raise ValueError(
            f"ENCRYPTION_KEY must be 64 hex characters (32 bytes), "
            f"got {len(key_bytes) * 2}. "
            f"Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def encryption_enabled() -> bool:
    return bool(ENCRYPTION_KEY)


def encrypt_data(plaintext: str) -> str:
    """Encrypt a string with ENCRYPTION_KEY. Raises if key is not set or invalid."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_data(ciphertext: str) -> str:
    """Decrypt a Fernet token with ENCRYPTION_KEY. Raises if key is wrong or token is invalid."""
    return _fernet().decrypt(ciphertext.encode()).decode()


# ---------------------------------------------------------------------------
# Credential file I/O
# ---------------------------------------------------------------------------

def _write_creds_file(data: dict) -> None:
    """Write data to credentials.json, encrypting if ENCRYPTION_KEY is set."""
    path = _creds_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if ENCRYPTION_KEY:
        payload = {"_enc": True, "data": encrypt_data(json.dumps(data))}
    else:
        payload = data
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_credentials() -> dict:
    """Return credentials from credentials.json, or {} if the file does not exist."""
    global _creds
    if _creds is not None:
        return _creds

    path = _creds_path()
    try:
        with open(path) as f:
            stored = json.load(f)

        if "_enc" in stored:
            if not ENCRYPTION_KEY:
                raise RuntimeError(
                    "credentials.json is encrypted but ENCRYPTION_KEY is not set in "
                    "docker-compose.yml. Restore the key or re-run setup at /setup."
                )
            try:
                data = json.loads(decrypt_data(stored["data"]))
            except Exception:
                raise RuntimeError(
                    "Failed to decrypt credentials.json — ENCRYPTION_KEY may have changed. "
                    "Re-run setup at /setup in the admin UI to re-configure."
                )
        else:
            data = stored

        _creds = data
        logger.debug("NinjaOne credentials loaded from %s", path)
        return _creds

    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # No credentials file — bot starts unconfigured; complete setup at /setup
    _creds = {}
    return _creds


# ---------------------------------------------------------------------------
# Slack config helpers (same encrypted-file pattern as NinjaOne credentials)
# ---------------------------------------------------------------------------

def _slack_config_path() -> str:
    return os.path.join(data_dir(), "slack_config.json")


def load_slack_config() -> dict:
    """Return Slack tokens from slack_config.json, or {} if not yet configured."""
    path = _slack_config_path()
    try:
        with open(path) as f:
            stored = json.load(f)
        if "_enc" in stored:
            if not ENCRYPTION_KEY:
                return {}
            return json.loads(decrypt_data(stored["data"]))
        return stored
    except (FileNotFoundError, Exception):
        return {}


def save_slack_config(data: dict) -> None:
    """Persist Slack tokens to slack_config.json."""
    path = _slack_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = (
        {"_enc": True, "data": encrypt_data(json.dumps(data))}
        if ENCRYPTION_KEY else data
    )
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.info("Slack config saved to %s", path)


def is_slack_configured() -> bool:
    cfg = load_slack_config()
    return bool(cfg.get("bot_token") and cfg.get("app_token"))


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """Return True if all required NinjaOne credentials are present."""
    creds = _load_credentials()
    return all(creds.get(k) for k in ("client_id", "client_secret", "api_base", "refresh_token"))


def get_api_base() -> str:
    """Return the NinjaOne API base URL (e.g. https://app.ninjarmm.com)."""
    return _load_credentials().get("api_base", "")


def save_credentials(data: dict) -> None:
    """
    Persist credentials and refresh in-memory state.
    Called by the web UI setup flow after a successful OAuth exchange.
    """
    global _creds
    _write_creds_file(data)
    _creds = data
    # Reset token cache so the next call authenticates with the new credentials
    _token["access_token"]  = ""
    _token["expires_at"]    = 0.0
    _token["refresh_token"] = data.get("refresh_token", "")
    logger.info("NinjaOne credentials saved")


def _persist_refresh_token(new_rt: str) -> None:
    """Write a rotated refresh token back to the credentials file."""
    global _creds
    creds = _load_credentials().copy()
    creds["refresh_token"] = new_rt
    try:
        _write_creds_file(creds)
        if _creds is not None:
            _creds["refresh_token"] = new_rt
    except OSError as exc:
        logger.warning("Could not persist rotated refresh token: %s", exc)


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

async def get_ninja_token(client: httpx.AsyncClient) -> str:
    """Return a valid NinjaOne bearer token, refreshing via refresh_token grant if needed."""
    if time.time() < _token["expires_at"] - 60:
        return _token["access_token"]

    creds = _load_credentials()
    required = ("client_id", "client_secret", "api_base", "refresh_token")
    if not all(creds.get(k) for k in required):
        raise RuntimeError(
            "NinjaOne credentials not configured — complete setup at /setup in the admin UI"
        )

    # Seed refresh token from creds on first use
    if not _token["refresh_token"]:
        _token["refresh_token"] = creds["refresh_token"]

    token_url = f"{creds['api_base']}/ws/oauth/token"

    logger.info("Refreshing NinjaOne access token")
    resp = await client.post(
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type":    "refresh_token",
            "client_id":     creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": _token["refresh_token"],
        },
    )
    resp.raise_for_status()
    data = resp.json()

    _token["access_token"] = data["access_token"]
    _token["expires_at"]   = time.time() + data.get("expires_in", 3600)

    if "refresh_token" in data and data["refresh_token"] != _token["refresh_token"]:
        new_rt = data["refresh_token"]
        _token["refresh_token"] = new_rt
        logger.info("NinjaOne refresh token rotated — persisting to credentials file")
        _persist_refresh_token(new_rt)

    logger.info("NinjaOne access token refreshed")
    return _token["access_token"]


async def ninja_headers(client: httpx.AsyncClient) -> dict:
    token = await get_ninja_token(client)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
