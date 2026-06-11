# Engineering Assistance Bot

Slack bot that standardises engineering assistance requests for the IntegrationOps team. Support reps submit a form via slash command; the bot creates a NinjaOne ticket and posts a public message in the channel. Engineering updates on the ticket relay automatically as thread replies. No bumping required.

The bot is **multi-tenant**: one Docker container can serve multiple independent NinjaOne + Slack workspaces, each with isolated credentials, commands, and ticket history.

---

## The flow

### 1 — Before you submit

Exhaust self-serve first. If any of these resolve the issue, close the Zendesk ticket with the relevant Dojo article linked and stop:

- Search the Dojo and review integration articles and release notes
- Confirm the integration is on the latest GA release
- Work through the issue with your support tier (L1 → LX)

### 2 — Submit the request

Run the slash command (e.g. `/request-assist`) in Slack. A form opens with fields defined by the linked NinjaOne ticket form — see [Architecture: Form generation](#form-generation) for how the Slack form is built from the NinjaOne schema.

By submitting you confirm: self-serve is exhausted, the issue is reproducible (or steps to reproduce are documented), and the submission is complete.

On submit the bot creates a NinjaOne ticket and posts a public message in the channel. All subsequent engineering updates on the ticket (comments, status changes) appear automatically as thread replies on that message — see [Architecture: Polling loop](#polling-loop) for details.

### 3 — Triage (Engineering/PM — within 24 hours)

**Problem ticket check:** Engineering searches for an open Problem Ticket matching the integration type and error signature.

- **Match found:** Zendesk case is linked to the existing Problem Ticket as a subscriber. No new investigation is opened.
- **Version gate:** If the client is below the current GA release, the request is returned — upgrade and resubmit.
- **No match:** Triage decision within 24 hours:

| Outcome | Action |
|---|---|
| Resolved at triage | Closed with Dojo link or explanation. Target: 80% of submissions. |
| Accepted | Problem Ticket opened, investigation begins (Phase 3). |
| Returned | Incomplete submission, outdated version, or self-serve not exhausted. |

### 4 — Investigation (Engineering)

A Problem Ticket in NinjaOne is the single source of truth. Additional cases reporting the same issue are linked as subscribers rather than opening new investigations. Subscriber count and device impact drive prioritisation.

**6-month activity rule:** Problem Tickets must show activity (new subscriber or substantive comment) every 6 months or they are automatically closed as "Insufficient Signal."

### 5 — Prioritisation (Engineering + PM — weekly)

Open Problem Tickets are reviewed weekly and ranked by subscriber count and device impact.

**Fast-track:** ≥ 500 affected devices → bypasses the weekly cycle and goes directly to sprint triage.

| Finding | Action |
|---|---|
| Bug confirmed | Mirrored to Jira. Priority set by subscriber count and device impact. |
| Customisation conflict | Closed as "Unsupported Configuration." PS engagement offered. |
| Feature request | Moved to Product Backlog via Productboard. |
| Insufficient data | Returned to support with specific data request. SLA paused. |

All decisions are logged as a comment on the Problem Ticket, which the bot relays back to the original Slack thread (see [Polling loop](#polling-loop)).

---

## Architecture

### Multi-tenant model

One process, N independent tenants. Each tenant has its own NinjaOne instance, Slack workspace, slash command registry, and ticket history. Tenants are completely isolated at the data layer.

**Auth model — three layers:**

```
Network boundary    Cloudflare Access (or SSH tunnel) — blocks unauthenticated
                    access at the host level before any request reaches the bot

Tenant discovery    Secret URL  /{url_secret}/
                    UUID v4 hex (~122 bits of entropy). Acts as both the tenant
                    identifier and the first authentication factor. Someone who
                    doesn't have the URL cannot reach that tenant's admin UI at
                    all — 404 is returned for any unknown url_secret.

Session auth        bcrypt password → HMAC-SHA256 signed session cookie (7-day TTL)
                    Second factor. The cookie payload is
                    "{tenant_id}:{expires_unix}:{hmac_sig}" — tamper-evident
                    without a database session store.
```

Why a secret URL rather than a separate subdomain per tenant: the bot is designed to run on a single small VPS. The entropy in a UUID v4 url_secret (~122 bits) is sufficient to make enumeration infeasible. No DNS changes, no per-tenant TLS certs, no nginx vhosts.

**Runtime model — one process, supervised tasks:**

```
main() in bot.py
  │
  ├─ TenantManager.load_all()       reads data/tenants.json
  ├─ TenantManager.start_all()      creates one Tenant object per record,
  │                                  starts a long-lived httpx.AsyncClient
  │
  └─ for each tenant:
       ├─ asyncio.create_task(_supervised_slack(tenant))
       │    └─ _run_slack_connection() → AsyncSocketModeHandler
       │       Restarts automatically after any crash (30s backoff)
       │
       ├─ asyncio.create_task(_supervised_poller(tenant))
       │    └─ run_poller(tenant)
       │       Restarts automatically after any crash (30s backoff)
       │
       └─ (all tasks share the event loop)

  └─ uvicorn.Server(admin_app).serve()   FastAPI admin UI, all routes under
                                          /{url_secret}/ prefix
```

One crashed tenant's Slack or poller task doesn't affect other tenants. Each supervised wrapper catches any exception, logs it with the tenant ID, and restarts after 30 seconds.

**Connection efficiency — long-lived HTTP client:**

Each `Tenant` object holds a single `httpx.AsyncClient(timeout=30)` that lives for the process lifetime. Every NinjaOne API call (token refresh, ticket creation, form schema fetch, polling) reuses this client. The old code opened a new `async with httpx.AsyncClient()` on every call, paying TCP + TLS handshake (~200–400ms) each time. The long-lived client amortises that cost to near-zero after the first connection.

**Data layout:**

```
data/
  tenants.json                     # url_secret → {id, name, admin_pw_hash, created_at}
  {tenant_id}/
    credentials.json               # NinjaOne OAuth credentials (encrypted if ENCRYPTION_KEY set)
    slack_config.json              # Slack bot + app tokens (encrypted if ENCRYPTION_KEY set)
    form_registry.json             # slash command → form mappings
    submissions.db                 # SQLite ticket tracking
    setup_pending.json             # transient OAuth state (deleted after callback)
```

The `url_secret` in `tenants.json` is the key — it never appears inside the value so it cannot be accidentally leaked from a file read. The `admin_pw_hash` is a bcrypt hash; the plaintext password is never stored.

**Key files introduced by multi-tenant refactor:**

| File | Role |
|---|---|
| `tenant.py` | `TenantRecord` (serialisable), `Tenant` (runtime state), `TenantManager` |
| `data/tenants.json` | Tenant registry (created on first `create-tenant` or auto-migration) |

**What was removed:**

- `signals.py` — replaced by `tenant.reconnect_event` (`asyncio.Event` per tenant). No global event bus.
- Module-level singletons in `ninja_auth.py` — replaced by `NinjaAuth` instances owned by each `Tenant`.
- Module-level caches in `bot.py` (`_user_cache`, `_form_schema_cache`) — replaced by `tenant.user_cache` and `tenant.form_schema_cache`.
- `async with httpx.AsyncClient()` at every call site — replaced by `tenant.http_client`.

---

### Form generation

The Slack modal is not a static form — every field is driven directly by the NinjaOne ticket form schema. Here is how it works:

```
Admin registers command → form ID stored in form_registry.json
        │
        ▼ (on slash command)
bot.py fetches /v2/ticketing/ticket-form/{id} from NinjaOne
        │
        ▼
schema_mapper.py converts each field definition to a Block Kit element:
  • text / textarea  → plain_text_input
  • dropdown / list  → static_select (options taken from the field schema)
  • checkbox         → checkboxes
  • date             → datepicker
        │
        ▼
Slack modal opens with all fields, labels, and dropdown options
populated directly from NinjaOne — no hardcoding in the bot
```

When the user submits:
- `schema_mapper.extract_values_from_submission()` maps Block Kit action IDs back to NinjaOne field IDs
- Submitted values are sent as `fields: [{id, value}]` in the ticket creation payload so they land as dedicated ticket attributes (filterable in NinjaOne ticketing), not just description text

Adding a new field type to a NinjaOne form, or changing dropdown options, automatically appears in Slack on the next modal open — no bot changes needed.

Field type detection checks `attributeType` first; `required` is determined by `technicianOption`/`endUserOption` in the field definition (not just the top-level `required` flag).

Multiple forms can be registered to the same slash command. When more than one form is registered, the bot presents a menu step before the ticket form so the submitter selects which form to fill.

### Polling loop

The bot tracks open tickets and relays engineering updates back to the originating Slack threads.

```
Support rep runs /request-assist
        │
        ▼
Bot opens Slack modal (fields from NinjaOne form schema — see above)
        │
        ▼ on submit
Creates NinjaOne ticket ──► Posts public message in channel
                                       │ (channel + message ts saved to SQLite)
                                       │
                         ┌─────────────┴──────────────┐
                         │  Poller (every 5 minutes)   │
                         │                             │
                         │  POST board/2/run           │
                         │  (ticket_changed filter)    │
                         │          │                  │
                         │  intersect with tracked IDs │
                         │          │                  │
                         │  GET log-entry?type=COMMENT │
                         │  &anchorId={last_seen_id}   │
                         └─────────────┬───────────────┘
                                       │ new comments / status changes
                                       ▼
                         Thread reply on original Slack post
```

The bot runs in Docker using Slack's Socket Mode — no inbound ports or public endpoints required. The poller makes two API calls per cycle regardless of how many open tickets are being tracked: one board run to get recently-changed IDs, then one log-entry call per matched ticket.

---

## Setup

### Prerequisites

- Docker + Docker Compose
- A NinjaOne instance with API access
- A Slack workspace where you can create apps

### 1. Create the Slack app

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From scratch**
2. **Settings → Socket Mode** → enable, create an app-level token with scope `connections:write` → copy the `xapp-` token
3. **Features → OAuth & Permissions → Bot Token Scopes** — add:
   `commands`, `chat:write`, `users:read`, `users:read.email`
4. **Features → Slash Commands** — create your commands (e.g. `/request-assist`). Leave Request URL blank — Socket Mode handles routing.
5. **Install App** → copy the `xoxb-` bot token

Keep these tokens — you will enter them in the admin web UI later.

### 2. NinjaOne API application

1. **Administration → Apps → API** → create an API application
2. Configure as follows — **all three settings are required**:
   - Grant type: **Authorization Code** (not Client Credentials or any other type)
   - Scopes: **monitoring**, **management**, and **offline_access**
   - Redirect URI: `https://your-host/{url_secret}/oauth/callback` — the exact value is shown on the setup page after the tenant is created
3. Note the Client ID and Client Secret

> **What goes wrong if misconfigured:**
> - Wrong grant type → token exchange fails with HTTP 400; setup page shows an error
> - Missing `offline_access` scope → NinjaOne does not issue a refresh token; the bot logs "NinjaOne did not return a refresh_token" and credentials cannot be saved
> - Missing `monitoring`/`management` → setup completes but ticket API calls return 403 Forbidden

### 3. Configure the environment

Copy `.env-template` to `.env` and fill in your values:

```bash
cp .env-template .env
```

| Variable | Description |
|---|---|
| `ADMIN_PORT` | Port for the admin web UI inside the container (default `8080`) |
| `ADMIN_BASE_URL` | Public base URL of the admin UI (e.g. `https://engassist.example.com`). Required when running behind a reverse proxy or Cloudflare Tunnel so the NinjaOne OAuth callback URL resolves correctly. Leave blank for direct `localhost` access. |
| `ENCRYPTION_KEY` | **Recommended.** 64-character hex key for encrypting `credentials.json` and `slack_config.json` at rest. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. If unset, credentials are stored in plaintext. |
| `HEARTBEAT_URL` | Optional Uptime Kuma push URL — leave blank to disable |
| `POLL_INTERVAL` | Seconds between poll cycles (default `300`) |
| `POLL_LOOKBACK_HOURS` | Board filter window in hours (default `1`, must be ≥ interval / 3600) |

> **Important:** back `ENCRYPTION_KEY` up separately from the `./data/` volume. If the key is lost, credentials cannot be recovered and both the NinjaOne and Slack setup must be re-run. Add `.env` to `.gitignore` — it should never be committed.

### 4. Start the container

```bash
docker compose up -d --build
docker compose logs -f
```

### 5. Create a tenant

Tenants are created via CLI inside the container. The bot must be running first.

```bash
docker compose exec form-bot python bot.py create-tenant --name "Your Org"
# Enter admin password when prompted
# Output: URL: https://your-host/{url_secret}/
```

The printed URL is the only copy of the `url_secret`. Save it — it cannot be recovered from the container (only its hash is stored). If lost, create a new tenant and re-run setup.

Restart the container after creating a tenant to start its Slack and poller tasks:

```bash
docker compose restart
```

### 6. Complete setup in the admin UI

Open the printed URL in a browser. You will be prompted to log in with the password you set in step 5.

Once logged in:

**Configure NinjaOne (one-time):**
1. Navigate to **Setup** (or follow the banner on the home page).
2. Copy the redirect URI shown on the setup page and register it in your NinjaOne API application (step 2). It will be `https://your-host/{url_secret}/oauth/callback`.
3. Enter your NinjaOne API base URL (e.g. `https://ca.ninjarmm.com`), Client ID, and Client Secret, then click **Authorise with NinjaOne**.
4. Approve the application in NinjaOne. You are redirected back and credentials are saved automatically.

**Configure Slack (one-time):**
1. Navigate to **Configure Slack** (or follow the banner).
2. Enter the `xoxb-` bot token and `xapp-` app-level token from step 1.
3. Click **Save tokens**. The bot connects to Slack automatically — no restart required.

**Register commands:**
Use the form on the home page to link each slash command to a NinjaOne ticket form and organisation. Multiple forms can be registered to one command — the bot will present a selection menu before the ticket form. Commands are active immediately after saving.

### Upgrading from single-tenant

If you have an existing single-tenant installation (a `data/credentials.json` at the top level), the bot performs a one-time automatic migration on first start:

1. A default tenant is created with a randomly generated password.
2. All legacy files (`credentials.json`, `slack_config.json`, `form_registry.json`, `submissions.db`, `setup_pending.json`) are moved to `data/{tenant_id}/`.
3. The admin URL and temporary password are printed to the log:

```bash
docker compose logs form-bot | grep "MIGRATION"
# MIGRATION url_secret=abc123...  password=xxx
```

Log in, then change the password by creating a new tenant if needed — the temporary password is one-time documentation, not a persistent credential.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Slack app — command router, modal handler, ticket creation, `main()` orchestrator, `create-tenant` CLI |
| `poller.py` | Background polling loop — board run → log-entry → Slack thread replies |
| `tenant.py` | `TenantRecord`, `Tenant`, `TenantManager` — multi-tenant lifecycle and data isolation |
| `db.py` | SQLite store — maps ticket IDs to Slack channel + message ts, tracks last-seen state |
| `ninja_auth.py` | NinjaOne OAuth2 token management + Slack config storage — per-tenant via `NinjaAuth` class |
| `schema_mapper.py` | Converts NinjaOne form field schemas into Slack Block Kit modal blocks |
| `registry.py` | Registry I/O — load/save `form_registry.json` with optional `data_dir` override |
| `web.py` | Admin web UI (FastAPI) — all routes under `/{url_secret}/` prefix; login, NinjaOne OAuth, Slack token setup, command management |
| `templates/` | Jinja2 HTML templates for the admin web UI |
| `pyproject.toml` | Python dependencies |
| `Dockerfile` | Python 3.12 + uv image |
| `docker-compose.yml` | Service definition — runtime constants only; secrets come from `.env` |
| `.env-template` | Template for the `.env` file — copy to `.env` and fill in values |

---

## Known gaps and next steps

### Before production

- **Session cookie `secure` flag** — the login cookie is currently set without `secure=True`. Behind HTTPS (Cloudflare Tunnel or nginx), the browser will send it over plain HTTP if a non-HTTPS path exists. Add `secure=True` to the `set_cookie` call in `web.py`'s `login_post` handler. This is safe to set when `ADMIN_BASE_URL` starts with `https://`.

- **`docker-compose.yml` stale env var** — `DB_PATH` in the compose file is a leftover from single-tenant. It is ignored in the current code (each tenant derives its own db path from `data/{tenant_id}/submissions.db`). It should be removed to avoid confusion.

- **Integration test** — the refactor has not been end-to-end tested. Minimum smoke test before cutting over:
  1. `create-tenant` → prints URL
  2. Visit URL → redirected to login
  3. Login with correct password → session cookie set
  4. Complete NinjaOne OAuth → callback URL contains url_secret, credentials saved
  5. Configure Slack tokens → bot connects without restart
  6. Register a slash command to a NinjaOne form
  7. Run the slash command in Slack → modal opens with correct fields
  8. Submit → NinjaOne ticket created, Slack message posted
  9. Add a comment to the NinjaOne ticket → appears as thread reply within one poll interval

- **Tenant restart after `create-tenant`** — the CLI creates the tenant record but doesn't start the Slack or poller tasks (those only start at process boot). A container restart is required after `create-tenant`. An improvement would be a `/_admin/tenants/reload` endpoint that starts tasks for newly created tenants without a restart.

- **NinjaOne redirect URI** — must be registered in the NinjaOne API application before clicking "Authorise". The setup page shows the exact URL including the `url_secret`. If you forget to register it first, NinjaOne will return an error on the OAuth redirect and you will need to re-initiate from the setup page.

### Product backlog

- **Zendesk ticket validation** — the form accepts any string. A future iteration should call the Zendesk API to verify the ticket exists before creating a NinjaOne ticket.

- **Duplicate submission guard** — implemented. When a second submission is received for the same Zendesk ticket ID, the bot attaches it to the existing NinjaOne ticket rather than creating a new one. Both Slack threads receive all subsequent engineering updates. Note: a thread that joins an already-active ticket does not receive backfill of earlier comments — it tracks from the cursor position at join time onwards.

- **Problem ticket matching at submission time** — search NinjaOne for an existing open Problem Ticket matching the integration type and surface it to the submitter before creating a new one.

- **Fast-track alert** — device count ≥ 500 should DM a designated channel immediately rather than waiting for the weekly review cycle.

- **DM fallback** — if the slash command is run from a DM, the bot replies with an error directing the user to a channel. A future improvement could open the form anyway and post the public ticket message into the relevant channel via a channel picker.

- **Tenant management UI** — currently tenants are created and deleted via CLI. An admin-of-admins UI (protected by `ADMIN_SECRET` or a separate credential) could list tenants, show last-active timestamps, and allow name changes.
