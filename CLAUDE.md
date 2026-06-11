# CLAUDE.md — Engineering Assistance Bot

## What this project is

A multi-tenant Slack bot that routes slash commands to NinjaOne ticket forms. Support reps run a slash command, fill a dynamically-generated modal (fields pulled live from the NinjaOne ticket form schema), and a NinjaOne ticket is created. The poller relays ticket comments and status changes back as Slack thread replies.

One process serves N independent tenants. Each tenant has its own NinjaOne instance, Slack workspace, command registry, and data directory.

## How to run

The app runs inside Docker. There is no local dev server — all meaningful testing requires the container.

```bash
# Build and start
docker compose up -d --build

# Tail logs
docker compose logs -f

# Create a tenant (container must be running)
docker compose exec form-bot python bot.py create-tenant --name "Acme"

# Restart after creating a tenant (required to activate Slack + poller tasks)
docker compose restart
```

Dependencies are installed by `uv` from `pyproject.toml` inside the container. There is no local venv to manage.

Syntax checking (no deps needed):
```bash
python3 -m py_compile bot.py poller.py web.py ninja_auth.py registry.py tenant.py db.py schema_mapper.py
```

## File map

| File | What it does |
|---|---|
| `bot.py` | Entry point. `main()` orchestrates TenantManager, starts supervised Slack + poller tasks per tenant, then serves the FastAPI admin app via uvicorn. Also contains the `create-tenant` CLI. |
| `tenant.py` | `TenantRecord` (serialized to `data/tenants.json`), `Tenant` (runtime state including `http_client`, caches, `reconnect_event`), `TenantManager`. |
| `web.py` | FastAPI admin UI. All routes on `tenant_router` mounted at `/{url_secret}`. Login, NinjaOne OAuth flow, Slack token setup, command management. |
| `poller.py` | `run_poller(tenant)` — polls NinjaOne every `POLL_INTERVAL` seconds, detects status changes and new comments, posts thread replies to Slack. |
| `ninja_auth.py` | `NinjaAuth` class — per-tenant NinjaOne OAuth2 credential management, token refresh, Slack config storage, and setup-pending state. Instantiated once per `Tenant`. |
| `schema_mapper.py` | Converts NinjaOne ticket form field schemas into Slack Block Kit blocks, and maps Block Kit submission values back to NinjaOne field IDs. |
| `registry.py` | Load/save `form_registry.json`. Accepts optional `data_dir` override for per-tenant isolation. |
| `db.py` | SQLite (aiosqlite) — two tables: `tickets` (one per NinjaOne ticket, holds polling cursor) and `threads` (one per Slack post, multiple threads per ticket for fan-out). All functions accept `db_path` param. |
| `templates/` | Jinja2 templates. Every template receives `url_secret` and uses it to prefix all links. |

## Architecture — key invariants

**Tenant isolation.** Every function that touches data receives either a `Tenant` object or an explicit `db_path`/`data_dir`. There are no module-level singletons for credentials, caches, or DB paths in the current code. `ninja_auth.py` has a `_default_auth` shim kept only for `registry.py`'s lazy import of `data_dir()` — do not expand it.

**Long-lived HTTP client.** Each `Tenant` holds one `httpx.AsyncClient(timeout=30)` that lives for the process lifetime. Never open `async with httpx.AsyncClient()` inside a hot path — pass `tenant.http_client` instead.

**Handler closures.** All Slack event handlers in `bot.py` are closures inside `_register_handlers(bolt_app, tenant: Tenant)`. They capture `tenant` and are registered once per tenant at startup. Do not add module-level Slack handlers.

**Supervised tasks.** `_supervised_slack` and `_supervised_poller` restart their inner functions after any exception (30s backoff). If you add a new long-running coroutine per tenant, wrap it in the same pattern.

**Reconnect event.** `tenant.reconnect_event` is an `asyncio.Event`. Set it (`tenant.reconnect_event.set()`) from the web UI to trigger a Slack reconnect. Do not use a global event bus — `signals.py` was deleted.

**Auth flow in web.py.** `get_tenant` resolves `url_secret` → `Tenant` (404 if unknown). `require_auth` wraps it and additionally validates the HMAC session cookie. The OAuth callback uses `get_tenant` only (no session — browser arrives from NinjaOne with no cookie). All other authenticated routes use `require_auth`.

**URL prefix.** Every admin route is under `/{url_secret}/`. Every `RedirectResponse` must include the prefix: `RedirectResponse(f"/{url_secret}/...")`. Every template context must include `"url_secret": url_secret`.

## Data layout

```
data/
  tenants.json                 # {url_secret: {id, name, admin_pw_hash, created_at}}
  {tenant_id}/
    credentials.json           # NinjaOne OAuth (encrypted if ENCRYPTION_KEY set)
    slack_config.json          # Slack tokens (encrypted if ENCRYPTION_KEY set)
    form_registry.json         # slash command → form mappings
    submissions.db             # SQLite
    setup_pending.json         # transient OAuth state
```

The `url_secret` is the key in `tenants.json` — it never appears inside the value dict.

## Environment variables

All resolved in `bot.py` constants at the top of the file and in `poller.py`:

| Variable | Default | Description |
|---|---|---|
| `ADMIN_PORT` | `8080` | Port uvicorn listens on |
| `ADMIN_BASE_URL` | `""` | Public base URL — required behind a reverse proxy for correct OAuth callback URLs |
| `ENCRYPTION_KEY` | `""` | 64-char hex key for encrypting credentials at rest |
| `POLL_INTERVAL` | `120` | Seconds between poller cycles |
| `POLL_LOOKBACK_HOURS` | `1` | Board filter window — must be ≥ `POLL_INTERVAL / 3600` |
| `HEARTBEAT_URL` | `""` | Uptime Kuma push URL |
| `LOG_FILE` | `"bot.log"` | Path to rotating log file |

`_SESSION_KEY` in `web.py` falls back to `ENCRYPTION_KEY`. If neither is set it uses `"dev-session-key"` — fine for local testing, never acceptable in production.

## NinjaOne API notes

- **OAuth grant type must be Authorization Code** — only this type issues a refresh token. Client Credentials will not work.
- **`offline_access` scope is required** — without it NinjaOne does not return a refresh token in the token exchange response.
- **Refresh token rotation** — NinjaOne rotates the refresh token on each use. `NinjaAuth.get_ninja_token()` persists the new token back to `credentials.json` automatically.
- **Field type detection** — `schema_mapper._resolve_field_type(field)` checks `attributeType` first. `_is_field_required(field)` checks `technicianOption` and `endUserOption` inside `content` — not just the top-level `required` flag.
- **Board endpoint** — the poller uses `POST /v2/ticketing/trigger/board/2/run` with a `ticket_changed` filter. Board ID 2 is not configurable from the UI — it is the default NinjaOne ticketing board.

## Slack notes

- **Socket Mode** — no inbound port, no public endpoint, no Request URL in the Slack app config.
- **One `AsyncApp` per tenant** — `_run_slack_connection` creates a fresh `AsyncApp` and `AsyncSocketModeHandler` each time it runs (including after reconnect). The handler is started with `await handler.start_async()` and disconnected by raising/returning from the function.
- **Reconnect** — saving Slack tokens in the web UI calls `tenant.reconnect_event.set()`. `_run_slack_connection` awaits this event in a loop; when set, it exits the current handler and re-enters with the new tokens.

## Schema mapper conventions

- `build_blocks_from_schema(form_schema, field_overrides)` — main entry point for building a modal. `field_overrides` is a dict of `{field_id: {label, placeholder, hint}}` set per command in the registry.
- `extract_values_from_submission(view, form_schema)` — maps Block Kit `values` back to `{field_id: value}` dict for ticket creation.
- `_field_to_block(field, field_overrides)` returns `None` for unsupported or hidden field types — callers must filter `None` from the block list.
- Block Kit `action_id` for each field is `f"field_{field['id']}"`.

## Registry format

`form_registry.json` structure:

```json
{
  "commands": {
    "/request-assist": [
      {
        "form_id": 12345,
        "org_id": 67890,
        "label": "Integration Issue",
        "field_overrides": {
          "42": {"label": "Zendesk Ticket #", "placeholder": "e.g. 12345"}
        }
      }
    ]
  }
}
```

A command maps to a list of entries. When the list has more than one entry, the bot presents a selection menu step before the ticket form. `org_id` is the NinjaOne client/organisation ID; `label` is shown in that menu.

## Things to avoid

- Do not add module-level state for per-tenant resources (caches, clients, tokens). It all lives on `Tenant`.
- Do not call `load_registry()` without a `data_dir` argument — the no-arg form uses the legacy `_default_auth` shim and will point at the wrong directory.
- Do not open a new `httpx.AsyncClient` inside handler or poller functions — use `tenant.http_client`.
- Do not add routes directly to `admin_app` with `@admin_app.get(...)` — all routes go on `tenant_router` so the `/{url_secret}` prefix is applied.
- Do not set `signals.py` back up — reconnect is handled via `tenant.reconnect_event`.
- Do not commit `.env` — it contains the encryption key.
