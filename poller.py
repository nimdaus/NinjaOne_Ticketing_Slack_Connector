"""
NinjaOne ticket poller.

Poll strategy (two API calls per cycle regardless of ticket count):

  1. POST /v2/ticketing/trigger/board/2/run
       — returns all tickets changed within POLL_LOOKBACK_HOURS.
       — we intersect the returned IDs against our submissions DB so we only
         act on tickets this bot created.

  2. GET /v2/ticketing/ticket/{id}/log-entry?type=COMMENT&anchorId={last_id}
       — returns only COMMENT entries added after the last one we've seen.
       — anchorId is the NinjaOne log-entry ID (int64), not a timestamp.

Status changes are detected by comparing the status field returned in the
board run response against the last-known status stored in the DB.

Environment variables
---------------------
POLL_INTERVAL          seconds between cycles (default 120)
POLL_LOOKBACK_HOURS    how far back the board filter looks (default 1)
                       should be >= POLL_INTERVAL / 3600, with headroom
"""

import asyncio
import logging
import os
import re

import httpx
from slack_sdk.web.async_client import AsyncWebClient

from db import get_all_open_tickets, get_threads_for_ticket, update_ticket_seen
from ninja_auth import get_api_base

logger = logging.getLogger("eng_assist_bot.poller")

SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
POLL_INTERVAL        = int(os.environ.get("POLL_INTERVAL", "120"))
POLL_LOOKBACK_HOURS  = int(os.environ.get("POLL_LOOKBACK_HOURS", "1"))

BOARD_ID = 2  # board that returns all tickets

# Log-entry types we care about relaying to Slack.
# COMMENT = engineer/technician comment, DESCRIPTION = original ticket body.
# SAVE entries carry changeDiff for field edits (including status) but are
# noisy and handled separately via the status field in the board response.
RELAY_TYPES = ["COMMENT"]

# NinjaOne status IDs that indicate a closed ticket.
# "4xxx" codes are the standard closed states; add instance-specific values here.
CLOSED_STATUSES = {"4000", "closed", "resolved", "complete", "completed"}

_slack = AsyncWebClient(token=SLACK_BOT_TOKEN)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_poller(get_ninja_token) -> None:
    """
    Infinite polling loop.  Accepts the bot's ``_get_ninja_token`` coroutine
    so the poller shares the same token cache and refresh logic.
    """
    logger.info(
        "Poller started — interval=%ds lookback=%dh",
        POLL_INTERVAL,
        POLL_LOOKBACK_HOURS,
    )
    while True:
        try:
            await _poll_cycle(get_ninja_token)
        except Exception:
            logger.exception("Unhandled error in poll cycle — will retry")
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Single poll cycle
# ---------------------------------------------------------------------------

async def _poll_cycle(get_ninja_token) -> None:
    tickets = await get_all_open_tickets()
    if not tickets:
        return

    tracked = {t["ninja_ticket_id"]: t for t in tickets}

    async with httpx.AsyncClient(timeout=30) as http:
        token = await get_ninja_token(http)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # Step 1 — board run: get recently changed ticket IDs + their status
        changed = await _board_run(http, headers)
        if not changed:
            logger.debug("Board run returned no recently changed tickets")
            return

        logger.debug(
            "Board run: %d recently changed ticket(s), %d tracked",
            len(changed),
            len(tracked),
        )

        # Step 2 — intersect and process only tickets we're tracking
        for ticket_id, board_row in changed.items():
            if ticket_id not in tracked:
                continue
            try:
                await _process_ticket(http, headers, tracked[ticket_id], board_row)
            except Exception:
                logger.exception("Error processing ticket %s", ticket_id)


# ---------------------------------------------------------------------------
# Board run
# ---------------------------------------------------------------------------

async def _board_run(http: httpx.AsyncClient, headers: dict) -> dict[str, dict]:
    """
    POST to board/2/run with a ticket_changed filter.

    Returns a dict mapping ticket_id (str) → board data row, so the caller
    can look up status without an extra API call.
    """
    # "1:0" means hours_since >= 1h 0m  — i.e. changed in the last hour.
    # We use POLL_LOOKBACK_HOURS so this stays correct if the interval is tuned.
    lookback_value = f"{POLL_LOOKBACK_HOURS}:0"

    payload = {
        "filters": [
            {
                "field": "ticket_changed",
                "operator": "hours_since:greater_or_equal_than",
                "value": lookback_value,
            }
        ],
        "includeColumns": ["id", "status", "subject"],
        "pageSize": 500,
    }

    try:
        resp = await http.post(
            f"{get_api_base()}/v2/ticketing/trigger/board/{BOARD_ID}/run",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("Board run failed (%s): %s", exc.response.status_code, exc)
        return {}

    data = resp.json()
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        logger.warning("Board run returned unexpected shape: %s", type(data))
        return {}

    result: dict[str, dict] = {}
    for row in rows:
        # Each row is a dict of column→value. The ticket ID is under "id".
        tid = row.get("id")
        if tid is not None:
            result[str(tid)] = row
    return result


# ---------------------------------------------------------------------------
# Per-ticket processing
# ---------------------------------------------------------------------------

async def _process_ticket(
    http: httpx.AsyncClient,
    headers: dict,
    ticket: dict,
    board_row: dict,
) -> None:
    ticket_id = ticket["ninja_ticket_id"]

    # Load all Slack threads watching this ticket for fan-out
    threads = await get_threads_for_ticket(ticket_id)
    if not threads:
        logger.debug("Ticket %s has no tracked threads, skipping", ticket_id)
        return

    # --- status change -------------------------------------------------------
    current_status = str(board_row.get("status") or board_row.get("statusId") or "").strip()
    last_status    = ticket.get("last_status") or ""

    if current_status and current_status != last_status:
        logger.info(
            "Ticket %s status: %r → %r (%d thread(s))",
            ticket_id, last_status, current_status, len(threads),
        )
        for thread in threads:
            await _post_status_update(thread, current_status, last_status, ticket_id)
        await update_ticket_seen(ticket_id, last_status=current_status)

        if _is_closed(current_status):
            logger.info("Ticket %s is closed — removing from poll queue", ticket_id)
            await update_ticket_seen(ticket_id, closed=True)
            return  # no need to fetch comments for a closed ticket

    # --- new comments --------------------------------------------------------
    anchor_id = ticket.get("last_activity_id") or None
    entries   = await _get_log_entries(http, headers, ticket_id, anchor_id)

    if not entries:
        return

    # Entries are returned oldest-first; relay in order, fan out to all threads
    newest_id = anchor_id
    for entry in entries:
        for thread in threads:
            await _post_log_entry(thread, entry, ticket_id)
        entry_id = entry.get("id")
        if entry_id is not None:
            newest_id = entry_id

    if newest_id != anchor_id:
        await update_ticket_seen(
            ticket_id,
            last_activity_id=str(newest_id),
            last_activity_ts=float(entries[-1].get("createTime") or 0),
        )


# ---------------------------------------------------------------------------
# NinjaOne: log entries
# ---------------------------------------------------------------------------

async def _get_log_entries(
    http: httpx.AsyncClient,
    headers: dict,
    ticket_id: str,
    anchor_id: str | None,
) -> list[dict]:
    """
    GET /v2/ticketing/ticket/{id}/log-entry

    Filters to RELAY_TYPES only.  Uses anchorId to return only entries
    added after the last one we've already relayed.
    """
    params: dict = {"pageSize": 200}
    for t in RELAY_TYPES:
        params.setdefault("type", [])
        params["type"].append(t)

    if anchor_id is not None:
        params["anchorId"] = int(anchor_id)

    try:
        resp = await http.get(
            f"{get_api_base()}/v2/ticketing/ticket/{ticket_id}/log-entry",
            headers=headers,
            params=params,
        )
        if resp.status_code == 404:
            logger.warning("Ticket %s not found", ticket_id)
            return []
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Log-entry fetch failed for ticket %s (%s)",
            ticket_id,
            exc.response.status_code,
        )
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_closed(status: str) -> bool:
    s = status.lower().strip()
    if s in CLOSED_STATUSES:
        return True
    # NinjaOne standard closed status IDs start with "4" and are numeric
    if s.startswith("4") and s.isdigit():
        return True
    return False


def _strip_html(text: str) -> str:
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.strip()


def _entry_body(entry: dict) -> str:
    """Return plain-text body from a log entry, preferring htmlBody → body."""
    html = entry.get("htmlBody") or ""
    plain = entry.get("body") or ""
    text = _strip_html(html) if html else plain.strip()
    return (text[:1200] + "…") if len(text) > 1200 else text


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------

async def _post_status_update(
    sub: dict, current_status: str, last_status: str, ticket_id: str
) -> None:
    ticket_url = f"{get_api_base()}/#/ticketing/ticket/{ticket_id}"
    link       = f"<{ticket_url}|#{ticket_id}>"
    change     = f"{last_status} → {current_status}" if last_status else current_status

    is_closed  = _is_closed(current_status)
    emoji      = ":white_check_mark:" if is_closed else ":arrows_counterclockwise:"
    label      = "Ticket closed" if is_closed else "Status updated"

    await _post_thread(
        sub,
        text=f"{label}: {change}",
        blocks=[{
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{emoji} *{label}:* {change}\n{link}"},
        }],
    )


async def _post_log_entry(sub: dict, entry: dict, ticket_id: str) -> None:
    body = _entry_body(entry)
    if not body:
        return  # nothing worth posting

    ticket_url = f"{get_api_base()}/#/ticketing/ticket/{ticket_id}"
    link       = f"<{ticket_url}|#{ticket_id}>"

    # public=True entries are visible to the requester in NinjaOne portal;
    # we relay all COMMENT entries regardless since support is the audience
    public = entry.get("publicEntry", True)
    scope  = "" if public else " _(internal)_"

    await _post_thread(
        sub,
        text=f"Update on ticket #{ticket_id}",
        blocks=[
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f":speech_balloon: *Engineering comment*{scope} on {link}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": body},
            },
        ],
    )


async def _post_thread(sub: dict, *, text: str, blocks: list) -> None:
    try:
        await _slack.chat_postMessage(
            channel=sub["slack_channel_id"],
            thread_ts=sub["slack_message_ts"],
            text=text,
            blocks=blocks,
        )
    except Exception as exc:
        logger.error(
            "Failed to post thread reply for ticket %s: %s",
            sub["ninja_ticket_id"],
            exc,
        )
