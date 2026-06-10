"""
Admin web UI — FastAPI + Jinja2.

Manages form-command mappings, NinjaOne credential setup, and Slack tokens.
Access control is handled by the upstream boundary (e.g. Cloudflare Access).

Routes
------
GET  /                        list registered commands
GET  /command/new             add-command form
POST /command/new             save new command
GET  /command/{cmd}/edit      edit-command form  (cmd is URL-encoded)
POST /command/{cmd}/edit      save edited command
POST /command/{cmd}/delete    delete a command
GET  /setup                   NinjaOne OAuth setup form
POST /setup/start             save credentials, redirect browser to NinjaOne for authorisation
GET  /oauth/callback          receive auth code from NinjaOne, exchange for tokens, save credentials
GET  /slack                   Slack token entry form
POST /slack                   save Slack bot + app tokens
"""

import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from registry import load_registry, save_registry
from signals import trigger_slack_reconnect
from ninja_auth import (
    ninja_headers, is_configured, get_api_base, save_credentials,
    data_dir, encrypt_data, decrypt_data, encryption_enabled,
    is_slack_configured, load_slack_config, save_slack_config,
)

ADMIN_BASE_URL = os.environ.get("ADMIN_BASE_URL", "").rstrip("/")

_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

admin_app = FastAPI(title="Eng Assist Admin", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Setup pending-state helpers (server-side; client never sees the secret)
# ---------------------------------------------------------------------------

def _pending_path() -> str:
    return os.path.join(data_dir(), "setup_pending.json")


def _write_pending(data: dict) -> None:
    path = _pending_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = (
        {"_enc": True, "data": encrypt_data(json.dumps(data))}
        if encryption_enabled() else data
    )
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_pending() -> dict | None:
    try:
        with open(_pending_path()) as f:
            stored = json.load(f)
        if "_enc" in stored:
            return json.loads(decrypt_data(stored["data"]))
        return stored
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _delete_pending() -> None:
    try:
        os.unlink(_pending_path())
    except FileNotFoundError:
        pass


def _callback_url(request: Request) -> str:
    """
    Return the absolute OAuth callback URL.

    Uses ADMIN_BASE_URL when set (required behind a reverse proxy or Cloudflare
    Tunnel where request.base_url reflects the internal address, not the public
    one that NinjaOne will redirect to).
    """
    if ADMIN_BASE_URL:
        return ADMIN_BASE_URL.rstrip("/") + "/oauth/callback"
    return f"{request.url.scheme}://{request.url.netloc}/oauth/callback"


# ---------------------------------------------------------------------------
# NinjaOne data helpers
# ---------------------------------------------------------------------------

async def _fetch_forms() -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as http:
        hdrs = await ninja_headers(http)
        resp = await http.get(f"{get_api_base()}/v2/ticketing/ticket-form", headers=hdrs)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


async def _fetch_orgs() -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as http:
        hdrs = await ninja_headers(http)
        resp = await http.get(f"{get_api_base()}/v2/organizations", headers=hdrs)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Command registry helpers
# ---------------------------------------------------------------------------

def _entry_to_list(raw) -> list[dict]:
    """Normalise a registry entry to a list regardless of whether it is a
    single dict (legacy) or already a list (multi-form)."""
    return raw if isinstance(raw, list) else [raw]


async def _parse_form_entries(form) -> list[dict]:
    """Build a list of form-entry dicts from indexed POST fields.

    Expected field names: label_N, ticket_form_id_N, ticket_form_name_N,
    client_id_N, client_name_N, default_subject_N  (N = 0-based index).
    entry_count controls how many indices are read.
    """
    count = max(1, int(form.get("entry_count") or "1"))
    entries = []
    for i in range(count):
        try:
            fid = int(form.get(f"ticket_form_id_{i}") or 0)
        except (ValueError, TypeError):
            fid = 0
        try:
            cid = int(form.get(f"client_id_{i}") or 0)
        except (ValueError, TypeError):
            cid = 0
        entry: dict = {
            "ticketFormId":   fid,
            "ticketFormName": (form.get(f"ticket_form_name_{i}") or ""),
            "clientId":       cid,
            "clientName":     (form.get(f"client_name_{i}") or ""),
            "defaultSubject": (form.get(f"default_subject_{i}") or "").strip(),
        }
        label = (form.get(f"label_{i}") or "").strip()
        if label:
            entry["label"] = label
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Routes — command management
# ---------------------------------------------------------------------------

@admin_app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    saved: str = "",
    deleted: str = "",
    error: str = "",
    setup: str = "",
    slack: str = "",
):
    registry = load_registry()
    commands = registry.get("commands", {})
    return _templates.TemplateResponse(request, "index.html", {
        "commands":         commands,
        "saved":            saved,
        "deleted":          deleted,
        "error":            error,
        "setup_done":       setup == "done",
        "slack_done":       slack == "done",
        "ninja_configured": is_configured(),
        "slack_configured": is_slack_configured(),
    })


@admin_app.get("/command/new", response_class=HTMLResponse)
async def new_command_form(
    request: Request,
    error: str = "",
):
    if not is_configured():
        return RedirectResponse("/setup", status_code=303)

    try:
        forms, orgs = await _fetch_forms(), await _fetch_orgs()
    except Exception as exc:
        forms, orgs = [], []
        error = f"Could not load NinjaOne data: {exc}"

    return _templates.TemplateResponse(request, "command_form.html", {
        "action":  "/command/new",
        "editing": False,
        "cmd":     "",
        "entries": [{}],
        "forms":   forms,
        "orgs":    orgs,
        "error":   error,
    })


@admin_app.post("/command/new")
async def new_command_save(request: Request):
    form = await request.form()
    cmd = (form.get("command") or "").strip()
    if not cmd.startswith("/"):
        cmd = "/" + cmd

    entries = await _parse_form_entries(form)
    registry = load_registry()
    registry.setdefault("commands", {})[cmd] = entries if len(entries) > 1 else entries[0]
    save_registry(registry)
    return RedirectResponse(f"/?saved={quote(cmd)}", status_code=303)


@admin_app.get("/command/{cmd:path}/edit", response_class=HTMLResponse)
async def edit_command_form(
    request: Request,
    cmd: str,
    error: str = "",
):
    cmd = unquote(cmd)
    registry = load_registry()
    raw = registry.get("commands", {}).get(cmd)
    if raw is None:
        raise HTTPException(status_code=404, detail=f"Command {cmd!r} not found")

    try:
        forms, orgs = await _fetch_forms(), await _fetch_orgs()
    except Exception as exc:
        forms, orgs = [], []
        error = f"Could not load NinjaOne data: {exc}"

    return _templates.TemplateResponse(request, "command_form.html", {
        "action":  f"/command/{quote(cmd, safe='')}/edit",
        "editing": True,
        "cmd":     cmd,
        "entries": _entry_to_list(raw),
        "forms":   forms,
        "orgs":    orgs,
        "error":   error,
    })


@admin_app.post("/command/{cmd:path}/edit")
async def edit_command_save(cmd: str, request: Request):
    cmd = unquote(cmd)
    registry = load_registry()
    if cmd not in registry.get("commands", {}):
        raise HTTPException(status_code=404, detail=f"Command {cmd!r} not found")

    form    = await request.form()
    entries = await _parse_form_entries(form)
    registry["commands"][cmd] = entries if len(entries) > 1 else entries[0]
    save_registry(registry)
    return RedirectResponse(f"/?saved={quote(cmd)}", status_code=303)


@admin_app.post("/command/{cmd:path}/delete")
async def delete_command(
    cmd: str,
):
    cmd = unquote(cmd)
    registry = load_registry()
    registry.get("commands", {}).pop(cmd, None)
    save_registry(registry)
    return RedirectResponse(f"/?deleted={quote(cmd)}", status_code=303)


# ---------------------------------------------------------------------------
# Routes — NinjaOne setup
# ---------------------------------------------------------------------------

@admin_app.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    error: str = "",
):
    return _templates.TemplateResponse(request, "setup.html", {
        "configured":   is_configured(),
        "callback_url": _callback_url(request),
        "error":        error,
    })


@admin_app.post("/setup/start")
async def setup_start(
    request: Request,
    api_base:      str = Form(...),
    client_id:     str = Form(...),
    client_secret: str = Form(...),
):
    api_base     = api_base.rstrip("/")
    redirect_uri = _callback_url(request)
    state        = secrets.token_urlsafe(16)

    auth_url = (
        f"{api_base}/ws/oauth/authorize"
        f"?response_type=code"
        f"&client_id={quote(client_id, safe='')}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&scope=monitoring+management"
        f"&state={state}"
    )
    _write_pending({
        "api_base":      api_base,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  redirect_uri,
        "state":         state,
    })
    # Redirect the browser directly to NinjaOne for authorisation
    return RedirectResponse(auth_url, status_code=303)


@admin_app.get("/oauth/callback")
async def oauth_callback(request: Request):
    """
    Receives the authorization code from NinjaOne after the user approves.
    No HTTP Basic Auth — the browser arrives here via redirect from NinjaOne.
    Security is provided by the state parameter matched against the pending file.
    """
    # NinjaOne may return an error (e.g. user denied access)
    error_param = request.query_params.get("error")
    if error_param:
        desc = request.query_params.get("error_description", error_param)
        return RedirectResponse(f"/setup?error={quote(desc)}", status_code=303)

    code  = request.query_params.get("code", "")
    state = request.query_params.get("state", "")

    if not code:
        return RedirectResponse(
            f"/setup?error={quote('No authorisation code received from NinjaOne.')}",
            status_code=303,
        )

    pending = _read_pending()
    if not pending:
        return RedirectResponse(
            f"/setup?error={quote('Setup session not found — please start over.')}",
            status_code=303,
        )

    if state != pending.get("state", ""):
        return RedirectResponse(
            f"/setup?error={quote('State mismatch — please start over.')}",
            status_code=303,
        )

    api_base      = pending["api_base"]
    client_id     = pending["client_id"]
    client_secret = pending["client_secret"]
    redirect_uri  = pending["redirect_uri"]

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(
                f"{api_base}/ws/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "redirect_uri":  redirect_uri,
                    "code":          code,
                },
            )

        try:
            resp_body = resp.json()
        except Exception:
            resp_body = None

        if resp.status_code != 200:
            detail = (
                resp_body.get("error_description") or resp_body.get("error") or resp.text[:500]
                if resp_body else resp.text[:500]
            )
            msg = f"Token exchange failed (HTTP {resp.status_code}): {detail}"
            return RedirectResponse(f"/setup?error={quote(msg)}", status_code=303)

        refresh_token = (resp_body or {}).get("refresh_token", "")

        if not refresh_token:
            body_preview = resp.text[:500] if not resp_body else str(resp_body)[:500]
            msg = (
                f"Token exchange succeeded (HTTP 200) but the response did not include a "
                f"refresh_token. Response body: {body_preview}"
            )
            return RedirectResponse(f"/setup?error={quote(msg)}", status_code=303)

        save_credentials({
            "client_id":     client_id,
            "client_secret": client_secret,
            "api_base":      api_base,
            "refresh_token": refresh_token,
        })
        _delete_pending()
        return RedirectResponse("/?setup=done", status_code=303)

    except Exception as exc:
        msg = f"Unexpected error during token exchange: {exc}"
        return RedirectResponse(f"/setup?error={quote(msg)}", status_code=303)


# ---------------------------------------------------------------------------
# Routes — Slack token setup
# ---------------------------------------------------------------------------

@admin_app.get("/slack", response_class=HTMLResponse)
async def slack_page(
    request: Request,
    error: str = "",
):
    cfg = load_slack_config()
    return _templates.TemplateResponse(request, "slack.html", {
        "configured": is_slack_configured(),
        "bot_token":  cfg.get("bot_token", ""),
        "app_token":  cfg.get("app_token", ""),
        "error":      error,
    })


@admin_app.post("/slack")
async def slack_save(
    bot_token: str = Form(...),
    app_token: str = Form(...),
):
    bot_token = bot_token.strip()
    app_token = app_token.strip()

    if not bot_token.startswith("xoxb-"):
        return RedirectResponse(
            f"/slack?error={quote('Bot token must start with xoxb-')}",
            status_code=303,
        )
    if not app_token.startswith("xapp-"):
        return RedirectResponse(
            f"/slack?error={quote('App-level token must start with xapp-')}",
            status_code=303,
        )

    save_slack_config({"bot_token": bot_token, "app_token": app_token})
    trigger_slack_reconnect()
    return RedirectResponse("/?slack=done", status_code=303)
