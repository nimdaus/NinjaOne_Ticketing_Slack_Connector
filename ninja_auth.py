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


# ---------------------------------------------------------------------------
# NinjaAuth class
# ---------------------------------------------------------------------------

class NinjaAuth:
    """Encapsulates NinjaOne OAuth2 credential management for one tenant."""

    def __init__(self, data_dir: str, encryption_key: str = ""):
        self._data_dir = data_dir
        self._encryption_key = encryption_key
        self._creds: dict | None = None
        self._token: dict = {
            "access_token":  "",
            "expires_at":    0.0,
            "refresh_token": "",
        }

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def _creds_path(self) -> str:
        return os.path.join(self._data_dir, "credentials.json")

    def _slack_config_path(self) -> str:
        return os.path.join(self._data_dir, "slack_config.json")

    def _pending_path(self) -> str:
        return os.path.join(self._data_dir, "setup_pending.json")

    # -----------------------------------------------------------------------
    # Encryption helpers
    # -----------------------------------------------------------------------

    def _fernet(self):
        from cryptography.fernet import Fernet
        try:
            key_bytes = bytes.fromhex(self._encryption_key)
        except ValueError:
            raise ValueError("ENCRYPTION_KEY contains invalid hex characters")
        if len(key_bytes) != 32:
            raise ValueError(
                f"ENCRYPTION_KEY must be 64 hex characters (32 bytes), "
                f"got {len(key_bytes) * 2}. "
                f"Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return Fernet(base64.urlsafe_b64encode(key_bytes))

    def encryption_enabled(self) -> bool:
        return bool(self._encryption_key)

    def encrypt_data(self, plaintext: str) -> str:
        """Encrypt a string with the encryption key. Raises if key is not set or invalid."""
        return self._fernet().encrypt(plaintext.encode()).decode()

    def decrypt_data(self, ciphertext: str) -> str:
        """Decrypt a Fernet token with the encryption key. Raises if key is wrong or token is invalid."""
        return self._fernet().decrypt(ciphertext.encode()).decode()

    # -----------------------------------------------------------------------
    # Credential file I/O
    # -----------------------------------------------------------------------

    def _write_creds_file(self, data: dict) -> None:
        """Write data to credentials.json, encrypting if encryption key is set."""
        path = self._creds_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if self._encryption_key:
            payload = {"_enc": True, "data": self.encrypt_data(json.dumps(data))}
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

    def _load_credentials(self) -> dict:
        """Return credentials from credentials.json, or {} if the file does not exist."""
        if self._creds is not None:
            return self._creds

        path = self._creds_path()
        try:
            with open(path) as f:
                stored = json.load(f)

            if "_enc" in stored:
                if not self._encryption_key:
                    raise RuntimeError(
                        "credentials.json is encrypted but ENCRYPTION_KEY is not set in "
                        "docker-compose.yml. Restore the key or re-run setup at /setup."
                    )
                try:
                    data = json.loads(self.decrypt_data(stored["data"]))
                except Exception:
                    raise RuntimeError(
                        "Failed to decrypt credentials.json — ENCRYPTION_KEY may have changed. "
                        "Re-run setup at /setup in the admin UI to re-configure."
                    )
            else:
                data = stored

            self._creds = data
            logger.debug("NinjaOne credentials loaded from %s", path)
            return self._creds

        except (FileNotFoundError, json.JSONDecodeError):
            pass

        # No credentials file — bot starts unconfigured; complete setup at /setup
        self._creds = {}
        return self._creds

    # -----------------------------------------------------------------------
    # Slack config helpers (same encrypted-file pattern as NinjaOne credentials)
    # -----------------------------------------------------------------------

    def load_slack_config(self) -> dict:
        """Return Slack tokens from slack_config.json, or {} if not yet configured."""
        path = self._slack_config_path()
        try:
            with open(path) as f:
                stored = json.load(f)
            if "_enc" in stored:
                if not self._encryption_key:
                    return {}
                return json.loads(self.decrypt_data(stored["data"]))
            return stored
        except (FileNotFoundError, Exception):
            return {}

    def save_slack_config(self, data: dict) -> None:
        """Persist Slack tokens to slack_config.json."""
        path = self._slack_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = (
            {"_enc": True, "data": self.encrypt_data(json.dumps(data))}
            if self.encryption_enabled() else data
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

    def is_slack_configured(self) -> bool:
        cfg = self.load_slack_config()
        return bool(cfg.get("bot_token") and cfg.get("app_token"))

    # -----------------------------------------------------------------------
    # Setup pending-state helpers
    # -----------------------------------------------------------------------

    def write_pending(self, data: dict) -> None:
        """Write OAuth pending state to disk."""
        path = self._pending_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = (
            {"_enc": True, "data": self.encrypt_data(json.dumps(data))}
            if self.encryption_enabled() else data
        )
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def read_pending(self) -> dict | None:
        try:
            with open(self._pending_path()) as f:
                stored = json.load(f)
            if "_enc" in stored:
                return json.loads(self.decrypt_data(stored["data"]))
            return stored
        except (FileNotFoundError, Exception):
            return None

    def delete_pending(self) -> None:
        try:
            os.unlink(self._pending_path())
        except FileNotFoundError:
            pass

    # -----------------------------------------------------------------------
    # Public helpers
    # -----------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True if all required NinjaOne credentials are present."""
        creds = self._load_credentials()
        return all(creds.get(k) for k in ("client_id", "client_secret", "api_base", "refresh_token"))

    def get_api_base(self) -> str:
        """Return the NinjaOne API base URL (e.g. https://app.ninjarmm.com)."""
        return self._load_credentials().get("api_base", "")

    def save_credentials(self, data: dict) -> None:
        """
        Persist credentials and refresh in-memory state.
        Called by the web UI setup flow after a successful OAuth exchange.
        """
        self._write_creds_file(data)
        self._creds = data
        # Reset token cache so the next call authenticates with the new credentials
        self._token["access_token"]  = ""
        self._token["expires_at"]    = 0.0
        self._token["refresh_token"] = data.get("refresh_token", "")
        logger.info("NinjaOne credentials saved")

    def _persist_refresh_token(self, new_rt: str) -> None:
        """Write a rotated refresh token back to the credentials file."""
        creds = self._load_credentials().copy()
        creds["refresh_token"] = new_rt
        try:
            self._write_creds_file(creds)
            if self._creds is not None:
                self._creds["refresh_token"] = new_rt
        except OSError as exc:
            logger.warning("Could not persist rotated refresh token: %s", exc)

    # -----------------------------------------------------------------------
    # Token management
    # -----------------------------------------------------------------------

    async def get_ninja_token(self, client: httpx.AsyncClient) -> str:
        """Return a valid NinjaOne bearer token, refreshing via refresh_token grant if needed."""
        if time.time() < self._token["expires_at"] - 60:
            return self._token["access_token"]

        creds = self._load_credentials()
        required = ("client_id", "client_secret", "api_base", "refresh_token")
        if not all(creds.get(k) for k in required):
            raise RuntimeError(
                "NinjaOne credentials not configured — complete setup at /setup in the admin UI"
            )

        # Seed refresh token from creds on first use
        if not self._token["refresh_token"]:
            self._token["refresh_token"] = creds["refresh_token"]

        token_url = f"{creds['api_base']}/ws/oauth/token"

        logger.info("Refreshing NinjaOne access token")
        resp = await client.post(
            token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type":    "refresh_token",
                "client_id":     creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": self._token["refresh_token"],
            },
        )
        resp.raise_for_status()
        data = resp.json()

        self._token["access_token"] = data["access_token"]
        self._token["expires_at"]   = time.time() + data.get("expires_in", 3600)

        if "refresh_token" in data and data["refresh_token"] != self._token["refresh_token"]:
            new_rt = data["refresh_token"]
            self._token["refresh_token"] = new_rt
            logger.info("NinjaOne refresh token rotated — persisting to credentials file")
            self._persist_refresh_token(new_rt)

        logger.info("NinjaOne access token refreshed")
        return self._token["access_token"]

    async def headers(self, client: httpx.AsyncClient) -> dict:
        token = await self.get_ninja_token(client)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Module-level singleton — used by single-tenant callers until Phase 3
# ---------------------------------------------------------------------------

_default_auth: NinjaAuth | None = None


def _get_default() -> NinjaAuth:
    global _default_auth
    if _default_auth is None:
        _default_auth = NinjaAuth(
            data_dir=os.path.dirname(
                os.environ.get(
                    "NINJA_CREDS_FILE",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "credentials.json"),
                )
            ),
            encryption_key=os.environ.get("ENCRYPTION_KEY", ""),
        )
    return _default_auth


# Keep the public ENCRYPTION_KEY constant for any direct importers
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")


def data_dir() -> str:
    return _get_default()._data_dir


