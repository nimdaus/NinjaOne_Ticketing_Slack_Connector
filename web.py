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

import hashlib
import hmac
import json
import os
import secrets
import time as _time
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from registry import load_registry, save_registry
from tenant import Tenant, TenantManager
from schema_mapper import _extract_fields_list, _resolve_field_type, _is_field_required

ADMIN_BASE_URL = os.environ.get("ADMIN_BASE_URL", "").rstrip("/")

_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

admin_app = FastAPI(title="Eng Assist Admin", docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# Session helpers — HMAC-signed cookie
# ---------------------------------------------------------------------------

_SESSION_KEY = os.environ.get("ENCRYPTION_KEY", "") or "dev-session-key"
_SESSION_TTL = 86400 * 7  # 7 days


def _make_session_cookie(tenant_id: str) -> str:
    expires = int(_time.time()) + _SESSION_TTL
    payload = f"{tenant_id}:{expires}"
    sig = hmac.new(_SESSION_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _valid_session(cookie: str | None, tenant_id: str) -> bool:
    if not cookie:
        return False
    try:
        parts = cookie.split(":", 2)
        if len(parts) != 3:
            return False
        tid, expires_str, sig = parts
        if tid != tenant_id:
            return False
        if int(expires_str) < int(_time.time()):
            return False
        payload = f"{tid}:{expires_str}"
        expected = hmac.new(_SESSION_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tenant dependency functions
# ---------------------------------------------------------------------------

async def get_tenant(url_secret: str, request: Request) -> Tenant:
    mgr: TenantManager = request.app.state.tenant_manager
    tenant = mgr.get(url_secret)
    if not tenant:
        raise HTTPException(404)
    return tenant


async def require_auth(
    url_secret: str,
    request: Request,
    tenant: Tenant = Depends(get_tenant),
) -> Tenant:
    session = request.cookies.get("session")
    if not _valid_session(session, tenant.id):
        raise HTTPException(303, headers={"Location": f"/{url_secret}/login"})
    return tenant


def _callback_url(request: Request, url_secret: str) -> str:
    """
    Return the absolute OAuth callback URL.

    Uses ADMIN_BASE_URL when set (required behind a reverse proxy or Cloudflare
    Tunnel where request.base_url reflects the internal address, not the public
    one that NinjaOne will redirect to).
    """
    base = ADMIN_BASE_URL or f"{request.url.scheme}://{request.url.netloc}"
    return f"{base}/{url_secret}/oauth/callback"


# ---------------------------------------------------------------------------
# NinjaOne data helpers
# ---------------------------------------------------------------------------

async def _fetch_forms(tenant: Tenant) -> list[dict]:
    hdrs = await tenant.ninja_auth.headers(tenant.http_client)
    resp = await tenant.http_client.get(
        f"{tenant.ninja_auth.get_api_base()}/v2/ticketing/ticket-form", headers=hdrs
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


async def _fetch_orgs(tenant: Tenant) -> list[dict]:
    hdrs = await tenant.ninja_auth.headers(tenant.http_client)
    resp = await tenant.http_client.get(
        f"{tenant.ninja_auth.get_api_base()}/v2/organizations", headers=hdrs
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Form-fields API — used by the admin UI's field-overrides panel
# ---------------------------------------------------------------------------

# Maps NinjaOne field type (uppercased) to the Slack element type string that
# appears in the field-overrides UI.  This is a UI-facing description of the
# *default* Slack widget; it intentionally diverges from _TYPE_MAP in a few
# places (e.g. EMAIL/URL/TIME/DATE_TIME all surface as plain_text_input here
# because their native Slack counterparts are not user-overridable).
_DEFAULT_SLACK_TYPE: dict[str, str] = {
    # Plain text
    "TEXT":                               "plain_text_input",
    "TEXTFIELD":                          "plain_text_input",
    "TEXT_FIELD":                         "plain_text_input",
    "SHORT_TEXT":                         "plain_text_input",
    "STRING":                             "plain_text_input",
    "PHONE":                              "plain_text_input",
    "IP_ADDRESS":                         "plain_text_input",
    "IPADDRESS":                          "plain_text_input",
    "TOTP":                               "plain_text_input",
    "ATTACHMENT":                         "plain_text_input",
    "EMAIL":                              "plain_text_input",
    "URL":                                "plain_text_input",
    "LINK":                               "plain_text_input",
    "TIME":                               "plain_text_input",
    # Multiline
    "WYSIWYG":                            "plain_text_input_multiline",
    "TEXTAREA":                           "plain_text_input_multiline",
    "LONG_TEXT":                          "plain_text_input_multiline",
    "MULTILINE":                          "plain_text_input_multiline",
    # Dropdowns
    "DROPDOWN":                           "static_select",
    "SELECT":                             "static_select",
    "ENUM":                               "static_select",
    "SINGLE_SELECT":                      "static_select",
    "DEVICE_DROPDOWN":                    "static_select",
    "ORGANIZATION_DROPDOWN":              "static_select",
    "ORGANIZATION_LOCATION_DROPDOWN":     "static_select",
    # Multi-select
    "MULTI_SELECT":                       "multi_static_select",
    "MULTI_DROPDOWN":                     "multi_static_select",
    "MULTISELECT":                        "multi_static_select",
    "DEVICE_MULTI_SELECT":                "multi_static_select",
    "ORGANIZATION_MULTI_SELECT":          "multi_static_select",
    "ORGANIZATION_LOCATION_MULTI_SELECT": "multi_static_select",
    # Boolean
    "CHECKBOX":                           "checkboxes",
    "BOOLEAN":                            "checkboxes",
    "BOOL":                               "checkboxes",
    # Date / datetime (datetimepicker is not user-overridable — surface as datepicker)
    "DATE":                               "datepicker",
    "DATEPICKER":                         "datepicker",
    "DATE_TIME":                          "datepicker",
    "DATETIME":                           "datepicker",
    # Numeric
    "NUMERIC":                            "number_input",
    "NUMBER":                             "number_input",
    "INTEGER":                            "number_input",
    "DECIMAL":                            "number_input",
}


tenant_router = APIRouter()


@tenant_router.get("/api/form-fields/{form_id}")
async def api_form_fields(form_id: int, tenant: Tenant = Depends(require_auth)) -> JSONResponse:
    """
    Return metadata for all visible fields in a NinjaOne ticket form.

    Used by the admin UI's field-overrides panel to populate the field list.
    """
    try:
        hdrs = await tenant.ninja_auth.headers(tenant.http_client)
        resp = await tenant.http_client.get(
            f"{tenant.ninja_auth.get_api_base()}/v2/ticketing/ticket-form/{form_id}",
            headers=hdrs,
        )
        resp.raise_for_status()
        form_schema = resp.json()
    except Exception as exc:
        return JSONResponse({"fields": [], "error": str(exc)})

    fields_raw = _extract_fields_list(form_schema)
    fields_out = []
    for field in fields_raw:
        # Skip hidden / system fields (same logic as _field_to_block)
        if field.get("hidden") or field.get("systemField"):
            continue

        field_id = str(
            field.get("id") or field.get("fieldId") or field.get("name", "")
        )
        label = str(
            field.get("label")
            or field.get("name")
            or field.get("displayName")
            or field_id
        )

        ninja_type = _resolve_field_type(field)
        default_slack_type = _DEFAULT_SLACK_TYPE.get(ninja_type, "plain_text_input")
        is_required = _is_field_required(field)

        fields_out.append({
            "id":               field_id,
            "label":            label,
            "ninjaType":        ninja_type,
            "defaultSlackType": default_slack_type,
            "required":         bool(is_required),
        })

    return JSONResponse({"fields": fields_out})


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

        raw_overrides = form.get(f"field_overrides_{i}") or ""
        if raw_overrides:
            try:
                parsed_overrides = json.loads(raw_overrides)
            except (ValueError, TypeError):
                parsed_overrides = {}
        else:
            parsed_overrides = {}
        if parsed_overrides:
            entry["fieldOverrides"] = parsed_overrides

        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Routes — command management
# ---------------------------------------------------------------------------

@tenant_router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(require_auth),
    saved: str = "",
    deleted: str = "",
    error: str = "",
    setup: str = "",
    slack: str = "",
):
    registry = load_registry(data_dir=tenant.data_dir)
    commands = registry.get("commands", {})
    return _templates.TemplateResponse(request, "index.html", {
        "commands":         commands,
        "saved":            saved,
        "deleted":          deleted,
        "error":            error,
        "setup_done":       setup == "done",
        "slack_done":       slack == "done",
        "ninja_configured": tenant.ninja_auth.is_configured(),
        "slack_configured": tenant.ninja_auth.is_slack_configured(),
        "url_secret":       url_secret,
    })


@tenant_router.get("/command/new", response_class=HTMLResponse)
async def new_command_form(
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(require_auth),
    error: str = "",
):
    if not tenant.ninja_auth.is_configured():
        return RedirectResponse(f"/{url_secret}/setup", status_code=303)

    try:
        forms, orgs = await _fetch_forms(tenant), await _fetch_orgs(tenant)
    except Exception as exc:
        forms, orgs = [], []
        error = f"Could not load NinjaOne data: {exc}"

    return _templates.TemplateResponse(request, "command_form.html", {
        "action":     f"/{url_secret}/command/new",
        "editing":    False,
        "cmd":        "",
        "entries":    [{}],
        "forms":      forms,
        "orgs":       orgs,
        "error":      error,
        "url_secret": url_secret,
    })


@tenant_router.post("/command/new")
async def new_command_save(
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(require_auth),
):
    form = await request.form()
    cmd = (form.get("command") or "").strip()
    if not cmd.startswith("/"):
        cmd = "/" + cmd

    entries = await _parse_form_entries(form)
    registry = load_registry(data_dir=tenant.data_dir)
    registry.setdefault("commands", {})[cmd] = entries if len(entries) > 1 else entries[0]
    save_registry(registry, data_dir=tenant.data_dir)
    return RedirectResponse(f"/{url_secret}/?saved={quote(cmd)}", status_code=303)


@tenant_router.get("/command/{cmd:path}/edit", response_class=HTMLResponse)
async def edit_command_form(
    request: Request,
    url_secret: str,
    cmd: str,
    tenant: Tenant = Depends(require_auth),
    error: str = "",
):
    cmd = unquote(cmd)
    registry = load_registry(data_dir=tenant.data_dir)
    raw = registry.get("commands", {}).get(cmd)
    if raw is None:
        raise HTTPException(status_code=404, detail=f"Command {cmd!r} not found")

    try:
        forms, orgs = await _fetch_forms(tenant), await _fetch_orgs(tenant)
    except Exception as exc:
        forms, orgs = [], []
        error = f"Could not load NinjaOne data: {exc}"

    return _templates.TemplateResponse(request, "command_form.html", {
        "action":     f"/{url_secret}/command/{quote(cmd, safe='')}/edit",
        "editing":    True,
        "cmd":        cmd,
        "entries":    _entry_to_list(raw),
        "forms":      forms,
        "orgs":       orgs,
        "error":      error,
        "url_secret": url_secret,
    })


@tenant_router.post("/command/{cmd:path}/edit")
async def edit_command_save(
    cmd: str,
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(require_auth),
):
    cmd = unquote(cmd)
    registry = load_registry(data_dir=tenant.data_dir)
    if cmd not in registry.get("commands", {}):
        raise HTTPException(status_code=404, detail=f"Command {cmd!r} not found")

    form    = await request.form()
    entries = await _parse_form_entries(form)
    registry["commands"][cmd] = entries if len(entries) > 1 else entries[0]
    save_registry(registry, data_dir=tenant.data_dir)
    return RedirectResponse(f"/{url_secret}/?saved={quote(cmd)}", status_code=303)


@tenant_router.post("/command/{cmd:path}/delete")
async def delete_command(
    cmd: str,
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(require_auth),
):
    cmd = unquote(cmd)
    registry = load_registry(data_dir=tenant.data_dir)
    registry.get("commands", {}).pop(cmd, None)
    save_registry(registry, data_dir=tenant.data_dir)
    return RedirectResponse(f"/{url_secret}/?deleted={quote(cmd)}", status_code=303)


# ---------------------------------------------------------------------------
# Routes — NinjaOne setup
# ---------------------------------------------------------------------------

@tenant_router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(require_auth),
    error: str = "",
):
    return _templates.TemplateResponse(request, "setup.html", {
        "configured":   tenant.ninja_auth.is_configured(),
        "callback_url": _callback_url(request, url_secret),
        "error":        error,
        "url_secret":   url_secret,
    })


@tenant_router.post("/setup/start")
async def setup_start(
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(require_auth),
    api_base:      str = Form(...),
    client_id:     str = Form(...),
    client_secret: str = Form(...),
):
    api_base     = api_base.rstrip("/")
    redirect_uri = _callback_url(request, url_secret)
    state        = secrets.token_urlsafe(16)

    auth_url = (
        f"{api_base}/ws/oauth/authorize"
        f"?response_type=code"
        f"&client_id={quote(client_id, safe='')}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&scope=monitoring+management+offline_access"
        f"&state={state}"
    )
    tenant.ninja_auth.write_pending({
        "api_base":      api_base,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  redirect_uri,
        "state":         state,
    })
    # Redirect the browser directly to NinjaOne for authorisation
    return RedirectResponse(auth_url, status_code=303)


@tenant_router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(get_tenant),
):
    """
    Receives the authorization code from NinjaOne after the user approves.
    No session cookie required — the browser arrives here via redirect from NinjaOne.
    Security is provided by the state parameter matched against the pending file.
    """
    # NinjaOne may return an error (e.g. user denied access)
    error_param = request.query_params.get("error")
    if error_param:
        desc = request.query_params.get("error_description", error_param)
        return RedirectResponse(f"/{url_secret}/setup?error={quote(desc)}", status_code=303)

    code  = request.query_params.get("code", "")
    state = request.query_params.get("state", "")

    if not code:
        return RedirectResponse(
            f"/{url_secret}/setup?error={quote('No authorisation code received from NinjaOne.')}",
            status_code=303,
        )

    pending = tenant.ninja_auth.read_pending()
    if not pending:
        return RedirectResponse(
            f"/{url_secret}/setup?error={quote('Setup session not found — please start over.')}",
            status_code=303,
        )

    if state != pending.get("state", ""):
        return RedirectResponse(
            f"/{url_secret}/setup?error={quote('State mismatch — please start over.')}",
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
            return RedirectResponse(f"/{url_secret}/setup?error={quote(msg)}", status_code=303)

        refresh_token = (resp_body or {}).get("refresh_token", "")

        if not refresh_token:
            body_preview = resp.text[:500] if not resp_body else str(resp_body)[:500]
            msg = (
                f"Token exchange succeeded (HTTP 200) but the response did not include a "
                f"refresh_token. Response body: {body_preview}"
            )
            return RedirectResponse(f"/{url_secret}/setup?error={quote(msg)}", status_code=303)

        tenant.ninja_auth.save_credentials({
            "client_id":     client_id,
            "client_secret": client_secret,
            "api_base":      api_base,
            "refresh_token": refresh_token,
        })
        tenant.ninja_auth.delete_pending()
        return RedirectResponse(f"/{url_secret}/?setup=done", status_code=303)

    except Exception as exc:
        msg = f"Unexpected error during token exchange: {exc}"
        return RedirectResponse(f"/{url_secret}/setup?error={quote(msg)}", status_code=303)


# ---------------------------------------------------------------------------
# Routes — Slack token setup
# ---------------------------------------------------------------------------

@tenant_router.get("/slack", response_class=HTMLResponse)
async def slack_page(
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(require_auth),
    error: str = "",
):
    cfg = tenant.ninja_auth.load_slack_config()
    return _templates.TemplateResponse(request, "slack.html", {
        "configured": tenant.ninja_auth.is_slack_configured(),
        "bot_token":  cfg.get("bot_token", ""),
        "app_token":  cfg.get("app_token", ""),
        "error":      error,
        "url_secret": url_secret,
    })


@tenant_router.post("/slack")
async def slack_save(
    request: Request,
    url_secret: str,
    tenant: Tenant = Depends(require_auth),
    bot_token: str = Form(...),
    app_token: str = Form(...),
):
    bot_token = bot_token.strip()
    app_token = app_token.strip()

    if not bot_token.startswith("xoxb-"):
        return RedirectResponse(
            f"/{url_secret}/slack?error={quote('Bot token must start with xoxb-')}",
            status_code=303,
        )
    if not app_token.startswith("xapp-"):
        return RedirectResponse(
            f"/{url_secret}/slack?error={quote('App-level token must start with xapp-')}",
            status_code=303,
        )

    tenant.ninja_auth.save_slack_config({"bot_token": bot_token, "app_token": app_token})
    tenant.reconnect_event.set()
    return RedirectResponse(f"/{url_secret}/?slack=done", status_code=303)


# ---------------------------------------------------------------------------
# Routes — auth (login / logout)
# ---------------------------------------------------------------------------

@tenant_router.get("/login", response_class=HTMLResponse)
async def login_page(
    url_secret: str,
    request: Request,
    error: str = "",
):
    tenant = await get_tenant(url_secret, request)
    if _valid_session(request.cookies.get("session"), tenant.id):
        return RedirectResponse(f"/{url_secret}/", status_code=303)
    return _templates.TemplateResponse(request, "login.html", {
        "url_secret": url_secret,
        "error":      error,
    })


@tenant_router.post("/login")
async def login_post(
    url_secret: str,
    request: Request,
):
    tenant = await get_tenant(url_secret, request)
    form = await request.form()
    password = str(form.get("password") or "")
    if not tenant.verify_password(password):
        return RedirectResponse(
            f"/{url_secret}/login?error={quote('Invalid password')}",
            status_code=303,
        )
    cookie_value = _make_session_cookie(tenant.id)
    response = RedirectResponse(f"/{url_secret}/", status_code=303)
    response.set_cookie(
        "session", cookie_value,
        httponly=True, samesite="lax", max_age=_SESSION_TTL,
    )
    return response


@tenant_router.get("/logout")
async def logout(url_secret: str):
    response = RedirectResponse(f"/{url_secret}/login", status_code=303)
    response.delete_cookie("session")
    return response


# ---------------------------------------------------------------------------
# Mount tenant router
# ---------------------------------------------------------------------------

admin_app.include_router(tenant_router, prefix="/{url_secret}")
