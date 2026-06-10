"""
Dynamic Slack Form Bot (v2.0)
Routes any registered slash command to a NinjaOne ticket form,
dynamically building the Slack modal from the form's field schema.

Configure commands via the admin web UI.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import re
import time

import httpx
import uvicorn
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from schema_mapper import build_blocks_from_schema, extract_values_from_submission
from registry import load_registry
from ninja_auth import get_ninja_token, ninja_headers as _ninja_headers_fn, get_api_base, load_slack_config
from signals import slack_reconnect_event
from db import init_db, save_submission, get_open_ticket_by_dedup_key
from poller import run_poller
from web import admin_app

# ---------------------------------------------------------------------------
# Configuration — all values sourced from environment (set in docker-compose.yml)
# ---------------------------------------------------------------------------

# Slack tokens: env vars take priority; fall back to /slack setup in admin UI
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
HEARTBEAT_URL   = os.environ.get("HEARTBEAT_URL", "")
ADMIN_PORT        = int(os.environ.get("ADMIN_PORT", "8080"))

# ---------------------------------------------------------------------------
# Logging — rotating file + console
# ---------------------------------------------------------------------------

LOG_FILE = os.environ.get("LOG_FILE", "bot.log")

logger = logging.getLogger("eng_assist_bot")
logger.setLevel(logging.DEBUG)

_fmt = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_fmt)

logger.addHandler(_file_handler)
logger.addHandler(_console_handler)

# ---------------------------------------------------------------------------
# Slack App
# ---------------------------------------------------------------------------

async def _ninja_headers(client: httpx.AsyncClient) -> dict:
    return await _ninja_headers_fn(client)


# ---------------------------------------------------------------------------
# NinjaOne helpers
# ---------------------------------------------------------------------------

# Simple in-memory cache for email → NinjaOne user ID lookups
_user_cache: dict[str, int | None] = {}


async def _lookup_ninja_user(client: httpx.AsyncClient, email: str) -> int | None:
    """Resolve a Slack user's email to a NinjaOne end-user ID."""
    if email in _user_cache:
        return _user_cache[email]

    headers = await _ninja_headers(client)
    try:
        resp = await client.get(
            f"{get_api_base()}/v2/users",
            headers=headers,
            params={"userType": "END_USER"},
        )
        resp.raise_for_status()
        for user in resp.json():
            if user.get("email", "").lower() == email.lower():
                _user_cache[email] = user["id"]
                logger.info("Mapped Slack email %s → NinjaOne user %s", email, user["id"])
                return user["id"]
    except httpx.HTTPStatusError as exc:
        logger.warning("NinjaOne user lookup failed (%s): %s", exc.response.status_code, exc)

    _user_cache[email] = None
    return None


# In-memory cache for form schemas: {form_id: {schema: ..., fetched_at: ...}}
_form_schema_cache: dict[int, dict] = {}
_SCHEMA_CACHE_TTL = 300  # 5 minutes


async def _fetch_form_schema(http: httpx.AsyncClient, form_id: int) -> dict:
    """Fetch and cache the ticket form schema from NinjaOne."""
    cached = _form_schema_cache.get(form_id)
    if cached and time.time() - cached["fetched_at"] < _SCHEMA_CACHE_TTL:
        return cached["schema"]

    headers = await _ninja_headers(http)
    resp = await http.get(
        f"{get_api_base()}/v2/ticketing/ticket-form/{form_id}",
        headers=headers,
    )
    resp.raise_for_status()
    schema = resp.json()
    _form_schema_cache[form_id] = {"schema": schema, "fetched_at": time.time()}
    logger.info("Fetched and cached form schema for form_id=%d", form_id)
    return schema


async def _create_ninja_ticket(
    http: httpx.AsyncClient,
    ticket_form_id: int,
    client_id: int,
    subject: str,
    description_body: str,
    requester_id: int | None,
    priority: str = "NONE",
    fields: list[dict] | None = None,
) -> dict:
    """
    Create a ticket in NinjaOne with the strict payload rules:
      - status: "1000" (string)
      - description.public: true (boolean)

    ``fields`` is a list of {"id": <int>, "value": <str>} dicts that map
    submitted values to dedicated NinjaOne ticket form fields, enabling
    filtering and reporting directly in NinjaOne ticketing.
    """
    headers = await _ninja_headers(http)

    payload: dict = {
        "clientId": client_id,
        "ticketFormId": ticket_form_id,
        "subject": subject[:255],
        "description": {
            "public": True,
            "body": description_body,
            "htmlBody": "<p>" + description_body.replace("\n", "<br>") + "</p>",
        },
        "status": "1000",
        "type": "PROBLEM",
        "priority": priority,
    }

    if requester_id:
        payload["requester"] = {"id": requester_id}

    if fields:
        # Coerce string IDs to int where possible — NinjaOne expects numeric IDs
        payload["fields"] = [
            {"id": _coerce_id(f["id"]), "value": f["value"]}
            for f in fields
        ]

    logger.debug("NinjaOne ticket payload: %s", json.dumps(payload, indent=2))

    resp = await http.post(
        f"{get_api_base()}/v2/ticketing/ticket",
        headers=headers,
        json=payload,
    )

    # Handle specific error codes
    if resp.status_code == 429:
        logger.error("NinjaOne rate limit hit (429)")
        raise NinjaRateLimitError("Rate limit exceeded — try again in a few minutes.")
    if resp.status_code in (400, 422):
        detail = resp.text
        logger.error("NinjaOne validation error (%s): %s", resp.status_code, detail)
        raise NinjaValidationError(f"Validation error: {detail}")

    resp.raise_for_status()
    ticket = resp.json()
    logger.info("NinjaOne ticket created: %s", ticket.get("id"))
    return ticket


def _coerce_id(value: str) -> int | str:
    """Return int if value is a numeric string, otherwise return it unchanged."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return value


class NinjaRateLimitError(Exception):
    pass


class NinjaValidationError(Exception):
    pass


# ---------------------------------------------------------------------------
# Uptime Kuma heartbeat
# ---------------------------------------------------------------------------


_HEALTHY_FILE = os.path.join(os.path.dirname(LOG_FILE), "healthy")


def _mark_healthy() -> None:
    """Touch the Docker healthcheck marker file."""
    try:
        os.makedirs(os.path.dirname(_HEALTHY_FILE) or ".", exist_ok=True)
        with open(_HEALTHY_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError as exc:
        logger.warning("Could not write healthcheck file: %s", exc)


async def _ping_heartbeat(client: httpx.AsyncClient) -> None:
    _mark_healthy()

    if not HEARTBEAT_URL:
        return
    try:
        resp = await client.get(HEARTBEAT_URL, timeout=10)
        logger.debug("Heartbeat ping: %s", resp.status_code)
    except Exception as exc:
        logger.warning("Heartbeat ping failed: %s", exc)


# ---------------------------------------------------------------------------
# Dynamic command handler — catch-all for registered commands
# ---------------------------------------------------------------------------


async def _prefetch_schemas(entries: list[dict]) -> None:
    """Warm the schema cache for all entries in a multi-form command."""
    async with httpx.AsyncClient(timeout=30) as http:
        for entry in entries:
            try:
                await _fetch_form_schema(http, entry["ticketFormId"])
            except Exception as exc:
                logger.warning("Schema prefetch failed form=%s: %s", entry.get("ticketFormId"), exc)


async def _build_form_modal(entry: dict, channel_id: str, cmd: str) -> dict | None:
    """Fetch the form schema, build Block Kit blocks, and return a modal dict.
    Returns None if the schema is unreachable or yields no blocks."""
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            form_schema = await _fetch_form_schema(http, entry["ticketFormId"])
    except Exception as exc:
        logger.error("Failed to fetch form schema form=%s: %s", entry.get("ticketFormId"), exc)
        return None

    blocks = build_blocks_from_schema(form_schema)
    if not blocks:
        logger.warning("No blocks generated for form %s", entry.get("ticketFormId"))
        return None

    title = (
        entry.get("label") or entry.get("ticketFormName")
        or cmd.lstrip("/").replace("-", " ").title()
    )[:24]

    return {
        "type": "modal",
        "callback_id": f"dynamic_form_submit_{entry['ticketFormId']}",
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps({
            "channel_id":     channel_id,
            "command":        cmd,
            "ticketFormId":   entry["ticketFormId"],
            "clientId":       entry["clientId"],
            "defaultSubject": entry.get("defaultSubject", ""),
        }),
        "blocks": blocks,
    }


async def handle_dynamic_command(ack, command, client):
    """Catch-all handler — opens a selector modal for multi-form commands,
    or the ticket form directly for single-form commands."""
    cmd        = command["command"]
    user_id    = command["user_id"]
    channel_id = command.get("channel_id", "")

    if channel_id.startswith("D"):
        await ack(f":no_entry_sign: `{cmd}` can only be used from a channel, not a direct message.")
        return

    registry = load_registry()
    config   = registry.get("commands", {}).get(cmd)

    if not config:
        await ack(
            f":question: No form is configured for `{cmd}`. "
            f"Ask an admin to register it via the admin web UI."
        )
        return

    await ack()

    entries = config if isinstance(config, list) else [config]

    logger.info(
        "Dynamic command: cmd=%s user=%s channel=%s entries=%d",
        cmd, user_id, channel_id, len(entries),
    )

    if len(entries) > 1:
        # Pre-warm schema cache while the user reads the selector
        asyncio.create_task(_prefetch_schemas(entries))

        options = [
            {
                "text": {
                    "type": "plain_text",
                    "text": (e.get("label") or e.get("ticketFormName") or f"Form {i + 1}")[:75],
                },
                "value": str(i),
            }
            for i, e in enumerate(entries)
        ]

        modal = {
            "type": "modal",
            "callback_id": "form_selector",
            "title": {"type": "plain_text", "text": cmd.lstrip("/")[:24]},
            "submit": {"type": "plain_text", "text": "Continue"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps({
                "channel_id": channel_id,
                "command":    cmd,
                "entries":    entries,
            }),
            "blocks": [
                {
                    "type": "input",
                    "block_id": "form_choice",
                    "label": {"type": "plain_text", "text": "What type of request is this?"},
                    "element": {
                        "type": "static_select",
                        "action_id": "selected_form",
                        "placeholder": {"type": "plain_text", "text": "Select a form type…"},
                        "options": options,
                    },
                }
            ],
        }
        await client.views_open(trigger_id=command["trigger_id"], view=modal)
        return

    # Single form — open the ticket form directly
    modal = await _build_form_modal(entries[0], channel_id, cmd)
    if modal is None:
        try:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=(
                    f":x: Could not load the form for `{cmd}`. "
                    f"Check the NinjaOne API connection and Form ID "
                    f"{entries[0].get('ticketFormId')}."
                ),
            )
        except Exception:
            pass
        return

    await client.views_open(trigger_id=command["trigger_id"], view=modal)


async def handle_form_selector_submission(ack, body, client, view):
    """Handles the selector modal — pushes the chosen ticket form on top."""
    metadata   = json.loads(view.get("private_metadata", "{}"))
    entries    = metadata.get("entries", [])
    channel_id = metadata.get("channel_id", "")
    cmd        = metadata.get("command", "")
    user_id    = body["user"]["id"]

    raw = view["state"]["values"]["form_choice"]["selected_form"].get("selected_option")
    if not raw:
        await ack({"response_action": "errors", "errors": {"form_choice": "Please select a form type."}})
        return

    selected_idx = int(raw["value"])
    if selected_idx >= len(entries):
        await ack({"response_action": "errors", "errors": {"form_choice": "Invalid selection — please try again."}})
        return

    entry = entries[selected_idx]
    modal = await _build_form_modal(entry, channel_id, cmd)

    if modal is None:
        await ack({
            "response_action": "errors",
            "errors": {
                "form_choice": (
                    f"Could not load form ID {entry.get('ticketFormId')}. "
                    f"Check the NinjaOne API connection."
                ),
            },
        })
        return

    logger.info(
        "Form selector: cmd=%s user=%s idx=%d form=%s",
        cmd, user_id, selected_idx, entry.get("ticketFormId"),
    )
    await ack({"response_action": "push", "view": modal})


# ---------------------------------------------------------------------------
# Dynamic view submission handler — matches any dynamic_form_submit_*
# ---------------------------------------------------------------------------


async def handle_dynamic_submission(ack, body, client, view):
    """
    Generic submission handler for all dynamically generated forms.
    Extracts field values, builds a ticket payload, and creates the ticket.
    """
    await ack()

    user_id = body["user"]["id"]
    values = view["state"]["values"]
    metadata = json.loads(view.get("private_metadata", "{}"))

    channel_id = metadata.get("channel_id", "")
    cmd = metadata.get("command", "?")
    ticket_form_id = metadata.get("ticketFormId")
    client_id = metadata.get("clientId")
    default_subject = metadata.get("defaultSubject", "")

    logger.info(
        "Dynamic form submission: cmd=%s user=%s form=%s",
        cmd,
        user_id,
        ticket_form_id,
    )

    # Process in background to avoid Slack timeout
    asyncio.create_task(
        _process_dynamic_submission(
            client, user_id, channel_id, cmd,
            ticket_form_id, client_id, default_subject, values,
        )
    )


async def _process_dynamic_submission(
    client,
    user_id: str,
    channel_id: str,
    cmd: str,
    ticket_form_id: int,
    client_id: int,
    default_subject: str,
    values: dict,
):
    """Create a NinjaOne ticket from the dynamic form submission."""
    async with httpx.AsyncClient(timeout=30) as http:
        try:
            # Fetch the form schema to map block IDs back to field labels
            form_schema = await _fetch_form_schema(http, ticket_form_id)

            # Extract submitted values — list of {id, label, value} per field
            submitted = extract_values_from_submission(values, form_schema)

            # Non-empty fields only
            filled_list = [f for f in submitted if f["value"]]

            # {label: value} for description building, subject templates, dedup, priority
            filled = {f["label"]: f["value"] for f in filled_list}

            # [{id, value}] for NinjaOne custom fields — only fields with numeric IDs
            # (system/name-keyed fields have no corresponding NinjaOne attribute)
            fields_payload = [
                {"id": f["id"], "value": f["value"]}
                for f in filled_list
                if f["id"].isdigit()
            ]

            # Build description body (human-readable summary always present)
            description_lines = [f"**{f['label']}:** {f['value']}" for f in filled_list]
            description_body = "\n\n".join(description_lines) or "(No fields were filled in)"

            # Build subject line
            if default_subject:
                # Template substitution: replace {field_name} with values
                subject = default_subject
                for label, value in filled.items():
                    # Try both exact match and lowercase/underscore variants
                    subject = subject.replace(f"{{{label}}}", value)
                    key_variant = label.lower().replace(" ", "_")
                    subject = subject.replace(f"{{{key_variant}}}", value)
                # If there are still unresolved placeholders, strip them
                subject = re.sub(r"\{[^}]+\}", "", subject).strip()
                if not subject:
                    subject = _auto_generate_subject(cmd, filled)
            else:
                subject = _auto_generate_subject(cmd, filled)

            # Resolve Slack email → NinjaOne user
            requester_id = None
            try:
                user_info = await client.users_info(user=user_id)
                email = user_info["user"]["profile"].get("email", "")
                if email:
                    requester_id = await _lookup_ninja_user(http, email)
            except Exception as exc:
                logger.warning("Could not resolve requester for user=%s: %s", user_id, exc)

            # Detect priority if a priority-like field was submitted
            priority = "NONE"
            for label, value in filled.items():
                if "priority" in label.lower():
                    if value.upper() in ("LOW", "NORMAL", "HIGH", "URGENT", "NONE"):
                        priority = value.upper()
                    break

            # Dedup: look for a Zendesk ticket field to use as the dedup key.
            # If an open NinjaOne ticket already exists for this Zendesk case,
            # attach this Slack thread to it instead of creating a duplicate.
            dedup_key = ""
            for label, value in filled.items():
                if "zendesk" in label.lower():
                    dedup_key = value.strip()
                    break
            if not dedup_key:
                logger.debug(
                    "No Zendesk field found in submission for cmd=%s — dedup skipped", cmd
                )

            existing = await get_open_ticket_by_dedup_key(dedup_key) if dedup_key else None
            is_duplicate = existing is not None

            if is_duplicate:
                ticket_id  = existing["ninja_ticket_id"]
                ticket_url = f"{get_api_base()}/#/ticketing/ticket/{ticket_id}"
                logger.info(
                    "Duplicate Zendesk ticket %s — attaching to existing NinjaOne ticket %s"
                    " | cmd=%s user=%s",
                    dedup_key, ticket_id, cmd, user_id,
                )
            else:
                ticket = await _create_ninja_ticket(
                    http,
                    ticket_form_id=ticket_form_id,
                    client_id=client_id,
                    subject=subject,
                    description_body=description_body,
                    requester_id=requester_id,
                    priority=priority,
                    fields=fields_payload,
                )
                ticket_id  = ticket.get("id")
                ticket_url = f"{get_api_base()}/#/ticketing/ticket/{ticket_id}"
                logger.info(
                    "Ticket created | cmd=%s | user=%s | ticket_id=%s",
                    cmd, user_id, ticket_id,
                )

            # Respond in the channel where the command was invoked
            if channel_id:
                # Ephemeral confirmation to the submitter
                if is_duplicate:
                    eph_text = (
                        f":link: *Your submission has been linked to an existing ticket*\n"
                        f"Ticket: <{ticket_url}|#{ticket_id}>\n"
                        f"Zendesk case {dedup_key} already has an open engineering ticket. "
                        f"You'll receive updates in this thread as engineering progresses."
                    )
                else:
                    eph_text = (
                        f":white_check_mark: *Ticket submitted successfully*\n"
                        f"Ticket: <{ticket_url}|#{ticket_id}>\n"
                        f"Form: _{cmd}_\n\n"
                        f"*Subject:* {subject}"
                    )

                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text=eph_text.split("\n")[0],
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": eph_text}}],
                )

                # Public message so NinjaOne updates can be threaded under it
                summary_fields = []
                for i, (label, value) in enumerate(filled.items()):
                    if i >= 4:
                        break
                    summary_fields.append(
                        {"type": "mrkdwn", "text": f"*{label}:*\n{value[:100]}"}
                    )

                if is_duplicate:
                    header_text  = f":link: Additional submission — {cmd}"
                    fallback_text = f"Additional submission from {cmd} by <@{user_id}> linked to #{ticket_id}"
                else:
                    header_text  = f":ticket: New ticket from {cmd}"
                    fallback_text = f"New ticket from {cmd} by <@{user_id}> — #{ticket_id}"

                notification_blocks = [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": header_text},
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Submitted by:*\n<@{user_id}>"},
                            {"type": "mrkdwn", "text": f"*Ticket:*\n<{ticket_url}|#{ticket_id}>"},
                        ],
                    },
                ]

                if summary_fields:
                    notification_blocks.append({"type": "section", "fields": summary_fields})

                notification_blocks.append({"type": "divider"})

                try:
                    notify_resp = await client.chat_postMessage(
                        channel=channel_id,
                        text=fallback_text,
                        blocks=notification_blocks,
                    )
                    if notify_resp.get("ok") and ticket_id:
                        await save_submission(
                            ninja_ticket_id=ticket_id,
                            slack_user_id=user_id,
                            slack_channel_id=channel_id,
                            slack_message_ts=notify_resp["ts"],
                            subject=subject,
                            command=cmd,
                            dedup_key=dedup_key,
                        )
                except Exception as chan_err:
                    logger.warning(
                        "Could not post notification in channel %s: %s",
                        channel_id,
                        chan_err,
                    )

            # Heartbeat on success
            await _ping_heartbeat(http)

        except NinjaRateLimitError:
            await _notify_error(
                client,
                channel_id,
                user_id,
                "NinjaOne is rate-limiting requests. Please try again in a few minutes.",
            )
        except NinjaValidationError as exc:
            await _notify_error(
                client,
                channel_id,
                user_id,
                f"NinjaOne rejected the request: {exc}",
            )
        except Exception as exc:
            logger.exception("Unexpected error processing submission for user=%s cmd=%s", user_id, cmd)
            await _notify_error(
                client,
                channel_id,
                user_id,
                f"Something went wrong creating your ticket. Engineering has been notified.\nError: {exc}",
            )


def _auto_generate_subject(cmd: str, filled: dict[str, str]) -> str:
    """Auto-generate a ticket subject from the command name and first few field values."""
    prefix = cmd.lstrip("/").replace("-", " ").title()
    # Use the first non-empty short field value as context
    context_parts = []
    for label, value in filled.items():
        if len(value) <= 120 and value:
            context_parts.append(value)
            if len(context_parts) >= 2:
                break
    context = " — ".join(context_parts) if context_parts else "New submission"
    return f"[{prefix}] {context}"[:255]


async def _notify_error(client, channel: str, user_id: str, message: str):
    """Send an ephemeral error message to the submitter."""
    if not channel:
        logger.error("No channel to send error to user=%s: %s", user_id, message)
        return
    try:
        await client.chat_postEphemeral(
            channel=channel,
            user=user_id,
            text=f":x: *Request failed*\n{message}",
        )
    except Exception as exc:
        logger.error("Failed to send error ephemeral to user=%s: %s", user_id, exc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _register_handlers(bolt_app) -> None:
    bolt_app.command(re.compile(r".*"))(handle_dynamic_command)
    bolt_app.view("form_selector")(handle_form_selector_submission)
    bolt_app.view(re.compile(r"^dynamic_form_submit_\d+"))(handle_dynamic_submission)


async def _run_slack_connection() -> None:
    """
    Manage the Slack Socket Mode connection for the lifetime of the process.

    Loops continuously so that:
      - If tokens are absent at startup, the loop waits until /slack saves them
        and web.py calls trigger_slack_reconnect().
      - After tokens are saved (or updated), the existing connection is closed
        and a fresh AsyncApp + AsyncSocketModeHandler is created with the new
        tokens — no container restart needed.
      - On connection failure, the loop backs off 30 s then retries.
    """
    event = slack_reconnect_event()
    current_handler: AsyncSocketModeHandler | None = None

    while True:
        cfg = load_slack_config()
        bot_token = SLACK_BOT_TOKEN or cfg.get("bot_token", "")
        app_token = SLACK_APP_TOKEN or cfg.get("app_token", "")

        if not bot_token or not app_token:
            logger.warning(
                "Slack tokens not configured. Enter them at /slack in the admin UI."
            )
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            continue

        # Disconnect the current handler before creating a new one
        if current_handler is not None:
            try:
                await current_handler.close_async()
                logger.info("Slack Socket Mode handler closed for reconnect")
            except Exception as exc:
                logger.warning("Error closing Socket Mode handler: %s", exc)
            current_handler = None

        try:
            bolt_app = AsyncApp(token=bot_token)
            _register_handlers(bolt_app)
            handler = AsyncSocketModeHandler(bolt_app, app_token)

            registry = load_registry()
            registered_cmds = list(registry.get("commands", {}).keys())
            logger.info(
                "Connecting to Slack (Socket Mode) | registered commands: %s",
                registered_cmds or "(none — configure via admin web UI)",
            )

            await handler.connect_async()
            _mark_healthy()
            logger.info("Slack Socket Mode connected")
            current_handler = handler

        except Exception as exc:
            logger.error("Failed to connect to Slack: %s", exc)
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            continue

        # Connection active — block until a reconnect is triggered
        event.clear()
        await event.wait()


async def main():
    await init_db()

    uvicorn_config = uvicorn.Config(
        admin_app,
        host="0.0.0.0",
        port=ADMIN_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)

    asyncio.create_task(_run_slack_connection())
    asyncio.create_task(run_poller(get_ninja_token))

    logger.info("Starting admin web UI on port %d", ADMIN_PORT)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
