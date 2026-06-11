"""
Tenant management — multi-tenant support.

Each tenant has:
  - url_secret  — UUID embedded in every admin URL (identifies + authenticates the tenant)
  - admin password — bcrypt-hashed, second factor for the admin UI session
  - data_dir    — isolated directory: data/{tenant_id}/

Data layout:
  data/
    tenants.json              # {url_secret: {id, name, admin_pw_hash, created_at}}
    {tenant_id}/
      credentials.json        # NinjaOne OAuth credentials
      slack_config.json       # Slack bot + app tokens
      form_registry.json      # slash command → form mappings
      submissions.db          # SQLite ticket tracking
"""

import asyncio
import json
import logging
import os
import secrets as _secrets
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from passlib.hash import bcrypt

from ninja_auth import NinjaAuth

logger = logging.getLogger("eng_assist_bot.tenant")

ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

# Base data directory — all tenant subdirectories live here
_BASE_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_TENANTS_FILE = os.path.join(_BASE_DATA_DIR, "tenants.json")

# Files that may exist in a legacy single-tenant installation
_LEGACY_FILES = [
    "credentials.json",
    "slack_config.json",
    "form_registry.json",
    "submissions.db",
    "setup_pending.json",
]


# ---------------------------------------------------------------------------
# TenantRecord — serializable
# ---------------------------------------------------------------------------

@dataclass
class TenantRecord:
    id: str
    url_secret: str
    name: str
    admin_pw_hash: str
    created_at: str

    @property
    def data_dir(self) -> str:
        return os.path.join(_BASE_DATA_DIR, self.id)

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "name":          self.name,
            "admin_pw_hash": self.admin_pw_hash,
            "created_at":    self.created_at,
        }

    @classmethod
    def from_dict(cls, url_secret: str, d: dict) -> "TenantRecord":
        return cls(
            id=d["id"],
            url_secret=url_secret,
            name=d.get("name", ""),
            admin_pw_hash=d.get("admin_pw_hash", ""),
            created_at=d.get("created_at", ""),
        )


# ---------------------------------------------------------------------------
# Tenant — runtime state, not serialized
# ---------------------------------------------------------------------------

class Tenant:
    """
    Runtime tenant state.

    Constructed by TenantManager.start_all(). Must be started inside the
    asyncio event loop (call await tenant.start()).
    """

    def __init__(self, record: TenantRecord):
        self.record = record
        self.ninja_auth = NinjaAuth(record.data_dir, ENCRYPTION_KEY)
        self.http_client: httpx.AsyncClient | None = None
        # Slack connections — set and managed by bot.py
        self.slack_app = None
        self.slack_handler = None
        self.reconnect_event: asyncio.Event = asyncio.Event()
        # Per-tenant in-memory caches (replace bot.py module-level globals)
        self.user_cache: dict[str, int | None] = {}
        self.form_schema_cache: dict[int, dict] = {}
        # Poller's Slack client — managed by poller.py
        self.slack_client = None

    # Forwarding properties for ergonomics
    @property
    def id(self) -> str:
        return self.record.id

    @property
    def url_secret(self) -> str:
        return self.record.url_secret

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def data_dir(self) -> str:
        return self.record.data_dir

    def db_path(self) -> str:
        return os.path.join(self.record.data_dir, "submissions.db")

    def verify_password(self, password: str) -> bool:
        try:
            return bcrypt.verify(password, self.record.admin_pw_hash)
        except Exception:
            return False

    async def start(self) -> None:
        """Initialize async resources. Call inside the running event loop."""
        os.makedirs(self.data_dir, exist_ok=True)
        self.http_client = httpx.AsyncClient(timeout=30)

    async def stop(self) -> None:
        """Release async resources."""
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None


# ---------------------------------------------------------------------------
# TenantManager
# ---------------------------------------------------------------------------

class TenantManager:
    def __init__(self):
        self._records: dict[str, TenantRecord] = {}  # keyed by url_secret
        self._tenants: dict[str, Tenant] = {}         # keyed by url_secret

    # ---- I/O ----------------------------------------------------------------

    def load_all(self) -> None:
        """Load TenantRecords from tenants.json. Safe to call before the event loop."""
        if not os.path.exists(_TENANTS_FILE):
            return
        try:
            with open(_TENANTS_FILE) as f:
                data = json.load(f)
            for url_secret, rec_dict in data.items():
                rec = TenantRecord.from_dict(url_secret, rec_dict)
                self._records[url_secret] = rec
            logger.info("Loaded %d tenant(s)", len(self._records))
        except Exception as exc:
            logger.error("Failed to load tenants.json: %s", exc)

    def save(self) -> None:
        """Persist all tenant records to tenants.json."""
        os.makedirs(_BASE_DATA_DIR, exist_ok=True)
        data = {secret: rec.to_dict() for secret, rec in self._records.items()}
        tmp = _TENANTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _TENANTS_FILE)
        logger.info("Saved %d tenant(s) to %s", len(data), _TENANTS_FILE)

    # ---- Tenant lifecycle ---------------------------------------------------

    def get(self, url_secret: str) -> Tenant | None:
        return self._tenants.get(url_secret)

    def get_all(self) -> list[Tenant]:
        return list(self._tenants.values())

    def create(self, name: str, password: str) -> TenantRecord:
        """
        Create a new tenant record, hash the password, create the data directory,
        and add it to the in-memory records. Call save() afterwards to persist.
        """
        tenant_id = uuid.uuid4().hex
        url_secret = uuid.uuid4().hex
        pw_hash = bcrypt.hash(password)
        created_at = datetime.now(timezone.utc).isoformat()
        rec = TenantRecord(
            id=tenant_id,
            url_secret=url_secret,
            name=name,
            admin_pw_hash=pw_hash,
            created_at=created_at,
        )
        os.makedirs(rec.data_dir, exist_ok=True)
        self._records[url_secret] = rec
        logger.info("Created tenant '%s' (id=%s)", name, tenant_id)
        return rec

    async def start_all(self) -> None:
        """Build Tenant objects and start async resources. Call inside the event loop."""
        for rec in self._records.values():
            t = Tenant(rec)
            await t.start()
            self._tenants[rec.url_secret] = t
        logger.info("Started %d tenant(s)", len(self._tenants))

    # ---- Migration ----------------------------------------------------------

    def migrate_legacy(self) -> TenantRecord | None:
        """
        One-time migration: if legacy data files exist in the base data dir
        but tenants.json does not, create a default tenant and move them.

        Returns the new TenantRecord if migration occurred, None otherwise.
        A random admin password is generated and printed to the log — the
        operator must read it from 'docker logs' on first startup.
        """
        if os.path.exists(_TENANTS_FILE):
            return None  # already multi-tenant

        legacy_present = any(
            os.path.exists(os.path.join(_BASE_DATA_DIR, f))
            for f in _LEGACY_FILES
        )
        if not legacy_present:
            return None  # fresh install, no migration needed

        # Generate a random one-time password for the migrated tenant
        temp_password = _secrets.token_urlsafe(16)
        rec = self.create("Default", temp_password)

        # Move legacy files into the new tenant's data directory
        for filename in _LEGACY_FILES:
            src = os.path.join(_BASE_DATA_DIR, filename)
            dst = os.path.join(rec.data_dir, filename)
            if os.path.exists(src):
                try:
                    shutil.move(src, dst)
                    logger.info("Migrated %s → %s", src, dst)
                except Exception as exc:
                    logger.warning("Could not migrate %s: %s", src, exc)

        logger.warning(
            "\n"
            "╔══════════════════════════════════════════════════════╗\n"
            "║  SINGLE-TENANT MIGRATION COMPLETE                    ║\n"
            "║                                                      ║\n"
            "║  Admin URL:  /{url_secret}/                          ║\n"
            "║  Password:   {password}                              ║\n"
            "║                                                      ║\n"
            "║  Save this password — it is not stored in plaintext. ║\n"
            "╚══════════════════════════════════════════════════════╝",
        )
        # Also log as plain text for easy grep
        logger.warning("MIGRATION url_secret=%s  password=%s", rec.url_secret, temp_password)

        return rec
