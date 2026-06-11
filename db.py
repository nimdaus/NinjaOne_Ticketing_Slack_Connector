"""
Submission store — aiosqlite-backed.

Two-table design:
  tickets  — one row per NinjaOne ticket, holds the shared polling cursor
             (last status, last activity anchor). Also stores the dedup_key
             (Zendesk ticket ID) so a second submission for the same Zendesk
             case attaches to the existing ticket instead of minting a new one.
  threads  — one row per Slack post. Multiple threads can reference the same
             ticket so engineering updates fan out to every submitter.

Migration: if the legacy single-table `submissions` schema is detected on
startup, rows are migrated into the new tables automatically.
"""

import os
import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "/app/data/submissions.db")


async def init_db(db_path: str = DB_PATH) -> None:
    """Create tables and migrate from the legacy single-table schema. Safe to call on every startup."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ninja_ticket_id   TEXT PRIMARY KEY,
                dedup_key         TEXT DEFAULT '',
                last_status       TEXT DEFAULT '',
                last_activity_id  TEXT DEFAULT '',
                last_activity_ts  REAL DEFAULT 0,
                closed            INTEGER DEFAULT 0,
                created_at        REAL DEFAULT (unixepoch())
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ninja_ticket_id   TEXT NOT NULL REFERENCES tickets(ninja_ticket_id),
                slack_user_id     TEXT NOT NULL,
                slack_channel_id  TEXT NOT NULL,
                slack_message_ts  TEXT NOT NULL,
                subject           TEXT DEFAULT '',
                command           TEXT DEFAULT '',
                created_at        REAL DEFAULT (unixepoch())
            )
        """)
        # Unique per Slack message so retries are idempotent
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_unique "
            "ON threads(slack_channel_id, slack_message_ts)"
        )
        # Fast dedup lookup
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_dedup "
            "ON tickets(dedup_key) WHERE dedup_key != ''"
        )

        await _migrate_from_submissions(db)
        await db.commit()


async def _migrate_from_submissions(db: aiosqlite.Connection) -> None:
    """One-time migration from the legacy submissions table. Idempotent."""
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='submissions'"
    ) as cur:
        if not await cur.fetchone():
            return

    await db.execute("""
        INSERT OR IGNORE INTO tickets
            (ninja_ticket_id, last_status, last_activity_id,
             last_activity_ts, closed, created_at)
        SELECT
            ninja_ticket_id,
            COALESCE(last_status, ''),
            COALESCE(last_activity_id, ''),
            COALESCE(last_activity_ts, 0),
            COALESCE(closed, 0),
            COALESCE(created_at, unixepoch())
        FROM submissions
    """)
    await db.execute("""
        INSERT OR IGNORE INTO threads
            (ninja_ticket_id, slack_user_id, slack_channel_id,
             slack_message_ts, subject, command, created_at)
        SELECT
            ninja_ticket_id, slack_user_id, slack_channel_id,
            slack_message_ts,
            COALESCE(subject, ''),
            COALESCE(command, ''),
            COALESCE(created_at, unixepoch())
        FROM submissions
    """)


async def save_submission(
    ninja_ticket_id: str | int,
    slack_user_id: str,
    slack_channel_id: str,
    slack_message_ts: str,
    subject: str = "",
    command: str = "",
    dedup_key: str = "",
    db_path: str = DB_PATH,
) -> None:
    """
    Register a Slack thread as a watcher for a NinjaOne ticket.

    If the ticket row already exists (a second submitter linked to the same
    Zendesk case), its polling cursor is left untouched — only a new threads
    row is added so future updates fan out to both Slack threads.
    """
    tid = str(ninja_ticket_id)
    async with aiosqlite.connect(db_path) as db:
        # Ensure the ticket row exists; preserve existing polling cursor.
        await db.execute(
            "INSERT OR IGNORE INTO tickets (ninja_ticket_id, dedup_key) VALUES (?, ?)",
            (tid, dedup_key),
        )
        # Always add a new thread row — one per Slack post.
        await db.execute(
            """
            INSERT OR IGNORE INTO threads
                (ninja_ticket_id, slack_user_id, slack_channel_id,
                 slack_message_ts, subject, command)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tid, slack_user_id, slack_channel_id, slack_message_ts, subject, command),
        )
        await db.commit()


async def get_open_ticket_by_dedup_key(key: str, db_path: str = DB_PATH) -> dict | None:
    """
    Return an open ticket whose dedup_key matches key, or None.

    A closed ticket is not returned — a re-opened Zendesk case that comes
    back months later should get a fresh NinjaOne ticket, not attach to a
    resolved one.
    """
    if not key:
        return None
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tickets WHERE dedup_key = ? AND closed = 0 LIMIT 1",
            (key,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_open_tickets(db_path: str = DB_PATH) -> list[dict]:
    """Return all ticket rows that have not been marked closed."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tickets WHERE closed = 0 ORDER BY created_at"
        ) as cursor:
            return [dict(row) async for row in cursor]


async def get_threads_for_ticket(ninja_ticket_id: str | int, db_path: str = DB_PATH) -> list[dict]:
    """Return all Slack thread rows for a given NinjaOne ticket."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM threads WHERE ninja_ticket_id = ? ORDER BY created_at",
            (str(ninja_ticket_id),),
        ) as cursor:
            return [dict(row) async for row in cursor]


async def update_ticket_seen(
    ninja_ticket_id: str | int,
    *,
    last_status: str | None = None,
    last_activity_id: str | None = None,
    last_activity_ts: float | None = None,
    closed: bool | None = None,
    db_path: str = DB_PATH,
) -> None:
    """Update the polling cursor for a ticket. Only provided fields are written."""
    fields, values = [], []
    if last_status is not None:
        fields.append("last_status = ?");      values.append(last_status)
    if last_activity_id is not None:
        fields.append("last_activity_id = ?"); values.append(last_activity_id)
    if last_activity_ts is not None:
        fields.append("last_activity_ts = ?"); values.append(last_activity_ts)
    if closed is not None:
        fields.append("closed = ?");           values.append(1 if closed else 0)
    if not fields:
        return
    values.append(str(ninja_ticket_id))
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"UPDATE tickets SET {', '.join(fields)} WHERE ninja_ticket_id = ?",
            values,
        )
        await db.commit()
