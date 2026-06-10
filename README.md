# Engineering Assistance Bot

Slack bot that standardises engineering assistance requests for the IntegrationOps team. Support reps submit a form via slash command; the bot creates a NinjaOne ticket and posts a public message in the channel. Engineering updates on the ticket relay automatically as thread replies. No bumping required.

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

### Polling loop

The bot tracks open tickets and relays engineering updates back to the originating Slack threads. See [The flow](#the-flow) step 2 for the user-facing view.

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

### 1. Create the Slack app

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From scratch**
2. **Settings → Socket Mode** → enable, create an app-level token with scope `connections:write` → copy the `xapp-` token
3. **Features → OAuth & Permissions → Bot Token Scopes** — add:
   `commands`, `chat:write`, `users:read`, `users:read.email`
4. **Features → Slash Commands** — create your commands (e.g. `/request-assist`). Leave Request URL blank — Socket Mode handles routing.
5. **Install App** → copy the `xoxb-` bot token

Keep these tokens handy — you will enter them in the admin web UI (step 4).

### 2. NinjaOne API application

1. **Administration → Apps → API** → create an API application
2. Configure as follows — **all three settings are required**:
   - Grant type: **Authorization Code** (not Client Credentials or any other type)
   - Scopes: **monitoring**, **management**, and **offline_access**
   - Redirect URI: `http://your-host:8080/oauth/callback` (exact value shown on the setup page)
3. Note the Client ID and Client Secret

> **What goes wrong if misconfigured:**
> - Wrong grant type → token exchange fails with HTTP 400; setup page shows an error
> - Missing scopes → setup completes but ticket API calls return 403 Forbidden
> - No refresh_token in response → usually means the wrong grant type is set; only *Authorization Code* issues a refresh token

### 3. Configure the environment

Copy `.env-template` to `.env` and fill in your values:

```bash
cp .env-template .env
```

| Variable | Description |
|---|---|
| `ADMIN_PORT` | Port for the admin web UI (default `8080`) |
| `ADMIN_BASE_URL` | Public base URL of the admin UI (e.g. `https://engassist.example.com`). Required when running behind a reverse proxy or Cloudflare Tunnel so the NinjaOne OAuth callback URL resolves correctly. Leave blank for direct `localhost` access. |
| `ENCRYPTION_KEY` | **Recommended.** 64-character hex key for encrypting `credentials.json` and `slack_config.json` at rest. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. If unset, credentials are stored in plaintext. |
| `HEARTBEAT_URL` | Optional Uptime Kuma push URL — leave blank to disable |
| `POLL_INTERVAL` | Seconds between poll cycles (default `300`) |
| `POLL_LOOKBACK_HOURS` | Board filter window in hours (default `1`, must be ≥ interval) |

> **Important:** back `ENCRYPTION_KEY` up separately from the `./data/` volume. If the key is lost, credentials cannot be recovered and both the NinjaOne and Slack setup must be re-run. Add `.env` to `.gitignore` — it should never be committed.

> **Access control:** the admin UI has no built-in authentication. Protect it at the network boundary — Cloudflare Access (zero-trust policy on the tunnel) or an SSH tunnel for local access are the recommended approaches. Do not expose the port directly to the internet.

### 4. Run

```bash
docker compose up -d --build
docker compose logs -f
```

The container starts in setup mode — no Slack tokens are loaded yet so the bot logs a warning and serves the admin web UI only.

### 5. Slack token setup (one-time, via admin web UI)

Open `http://your-host:8080` (or your public URL) in a browser.

1. Follow the **Configure Slack** banner on the home page (or navigate to **/slack**).
2. Enter the `xoxb-` bot token and `xapp-` app-level token from step 1.
3. Click **Save tokens**. The bot connects to Slack automatically — no restart required.

### 6. NinjaOne OAuth setup (one-time, via admin web UI)

1. Navigate to **Setup** (or follow the NinjaOne banner on the home page).
2. The setup page shows the redirect URI the bot will use. This must be registered in your NinjaOne API application (step 2) before proceeding.
3. Enter your NinjaOne API base URL (e.g. `https://ca.ninjarmm.com`), Client ID, and Client Secret, then click **Authorise with NinjaOne**.
4. Your browser is redirected to NinjaOne. Approve the application.
5. NinjaOne redirects back to the bot automatically. Credentials are saved to `/app/data/credentials.json` (persisted on the mounted volume) and the bot begins creating tickets immediately — no restart required.

### 7. Register commands

On the admin home page, use the form to link each slash command to a NinjaOne ticket form and organisation. Commands are active immediately after saving.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Slack app — command router, modal handler, ticket creation |
| `poller.py` | Background polling loop — board run → log-entry → Slack thread replies |
| `db.py` | SQLite store — maps ticket IDs to Slack channel + message ts, tracks last-seen state |
| `ninja_auth.py` | NinjaOne OAuth2 token management + Slack config storage — shared by bot and web UI |
| `schema_mapper.py` | Converts NinjaOne form field schemas into Slack Block Kit modal blocks |
| `registry.py` | Registry I/O — load/save `form_registry.json` |
| `web.py` | Admin web UI (FastAPI) — NinjaOne OAuth setup, Slack token setup, command management |
| `templates/` | Jinja2 HTML templates for the admin web UI |
| `form_registry.json` | Runtime command → form mappings (managed via admin web UI) |
| `pyproject.toml` | Python dependencies |
| `Dockerfile` | Python 3.12 + uv image |
| `docker-compose.yml` | Service definition — runtime constants only; secrets come from `.env` |
| `.env-template` | Template for the `.env` file — copy to `.env` and fill in values |

---

## Known gaps and next steps

- **Zendesk ticket validation** — the form accepts any string. A future iteration should call the Zendesk API to verify the ticket exists before creating a NinjaOne ticket.
- **Duplicate submission guard** — implemented. When a second submission is received for the same Zendesk ticket ID, the bot attaches it to the existing NinjaOne ticket rather than creating a new one. Both Slack threads receive all subsequent engineering updates. Note: a thread that joins an already-active ticket does not receive backfill of earlier comments — it tracks from the cursor position at join time onwards.
- **Problem ticket matching at submission time** — search NinjaOne for an existing open Problem Ticket matching the integration type and surface it to the submitter before creating a new one.
- **Fast-track alert** — device count ≥ 500 should DM a designated channel immediately rather than waiting for the weekly review cycle.
- **NinjaOne custom field mapping** — implemented. Submitted values are sent both as description text and as dedicated ticket field values (`fields: [{id, value}]`), enabling filtering and reporting in NinjaOne ticketing. Field options (dropdowns, selects) are sourced live from the NinjaOne ticket form schema — no hardcoding required. Note: if ticket creation returns a 400/422, check the logs for the detail — the `fields` payload shape may vary by NinjaOne instance.
- **DM fallback** — if the slash command is run from a DM, the bot replies with an error directing the user to a channel. A future improvement could open the form anyway and post the public ticket message into the relevant channel via a channel picker.
- **Hot-reload after Slack token setup** — implemented. Saving tokens via `/slack` immediately reconnects the bot without a container restart. The connection manager loop in `bot.py` wakes on a shared `asyncio.Event` (in `signals.py`) that `web.py` sets after each save.
