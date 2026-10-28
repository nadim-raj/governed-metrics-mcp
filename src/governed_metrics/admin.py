"""The admin console: grant and revoke access now, see what is pending reconciliation.

It sits behind the reverse proxy's single sign-on, never behind the MCP bearer token: a
browser session and an assistant's token are different credentials for different jobs.

* Identity: the proxy authenticates the administrator and forwards the verified identity
  in a header, together with a shared secret that proves the request came through the
  proxy (compared in constant time). Without the secret the console does not exist (404).
* Authorisation: the identity must be in the policy's `admins:` list, checked per request.
  Admin rights are separate from data access: the console shows grants, never data.
* Request forgery: the proxy's session cookie is sent on cross-site requests, so every
  POST must carry a matching Origin and an HMAC token bound to the administrator and
  issued within the last hour.
* Content: no scripts at all. A strict Content-Security-Policy, framing denied, every
  value HTML-escaped.

Console grants cover units only. Ad-hoc access, unmasked access and admin rights change
only through a reviewed change to the policy file.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import anyio

from . import overlay
from . import tools as T

if TYPE_CHECKING:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP

    from .runtime import Runtime

TOKEN_TTL_S = 3600
MAX_FORM_BYTES = 8192
MAX_REASON = 200
USER_ID = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,}$")
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; form-action 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
CSS = """\
:root{--fg:#1d1d1f;--muted:#5f6368;--line:#dadce0;--bg:#fff;--accent:#0b57d0;--warn:#b3261e}
@media (prefers-color-scheme:dark){:root{--fg:#e8eaed;--muted:#9aa0a6;--line:#3c4043;--bg:#202124;--accent:#8ab4f8;--warn:#f28b82}}
body{font:15px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);max-width:64rem;margin:2rem auto;padding:0 1rem}
h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:2rem}.muted{color:var(--muted)}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);padding:.35rem .5rem;text-align:left;vertical-align:top}
form{display:grid;gap:.5rem;max-width:32rem;border:1px solid var(--line);border-radius:8px;padding:1rem;margin:.5rem 0}
input[type=text]{font:inherit;padding:.35rem;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{font:inherit;padding:.45rem .8rem;border:0;border-radius:6px;background:var(--accent);color:var(--bg);cursor:pointer}
.warn{color:var(--warn)}.msg{padding:.5rem .75rem;border:1px solid var(--line);border-radius:6px}
"""


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


class Console:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.s = rt.settings
        self.origin = _origin(self.s.public_url)

    # ---- security checks -------------------------------------------------------------
    def identify(self, headers: Any) -> str | None:
        presented = headers.get("x-proxy-secret", "")
        if not hmac.compare_digest(presented.encode(), self.s.proxy_secret.encode()):
            return None
        user = headers.get(self.s.proxy_user_header, "").strip().lower()
        return user if user and self.rt.current_policy().is_admin(user) else None

    def came_via_proxy(self, headers: Any) -> bool:
        return hmac.compare_digest(headers.get("x-proxy-secret", "").encode(), self.s.proxy_secret.encode())

    def csrf_token(self, user: str, now: float | None = None) -> str:
        issued = int(time.time() if now is None else now)
        mac = hmac.new(self.s.csrf_secret.encode(), f"{user}|{issued}".encode(), hashlib.sha256).hexdigest()
        return f"{issued}.{mac}"

    def csrf_ok(self, user: str, token: str, now: float | None = None) -> bool:
        issued_s, _, mac = (token or "").partition(".")
        if not issued_s.isdigit() or not mac:
            return False
        age = int(time.time() if now is None else now) - int(issued_s)
        if not -60 <= age <= TOKEN_TTL_S:
            return False
        expected = self.csrf_token(user, now=int(issued_s)).partition(".")[2]
        return hmac.compare_digest(mac, expected)

    def origin_ok(self, headers: Any) -> bool:
        return headers.get("origin", "") == self.origin

    # ---- rendering -------------------------------------------------------------------
    def _decisions(self) -> Counter:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        try:
            return Counter((e.get("tool", "?"), e.get("outcome", "?")) for e in self.rt.audit.read(since))
        except OSError:
            return Counter()

    def page(self, user: str, message: str | None = None) -> str:
        e = html.escape
        rt, policy = self.rt, self.rt.current_policy()
        units = sorted(rt.knowledge.units)
        token = e(self.csrf_token(user))
        grants = "".join(
            f"<tr><td>{e(u)}</td><td>{e(', '.join(sorted(g.units)))}</td>"
            f"<td>{'yes' if g.adhoc is not None else 'no'}</td><td>{e(g.origin)}</td></tr>"
            for u, g in sorted(policy.grants.items()))
        pending = "".join(
            f"<tr><td>{e(u)}</td><td>{e(ev.get('action', ''))}</td>"
            f"<td>{e(', '.join(ev.get('units') or []))}</td><td>{e(str(ev.get('by', '')))}</td>"
            f"<td>{e(str(ev.get('at', '')))}</td><td>{e(str(ev.get('reason', '')))}</td></tr>"
            for u, ev in sorted(overlay.effective(self.s.overlay_path).items()))
        decisions = "".join(f"<tr><td>{e(t)}</td><td>{e(o)}</td><td>{n}</td></tr>"
                            for (t, o), n in sorted(self._decisions().items()))
        unit_boxes = "".join(f'<label><input type="checkbox" name="units" value="{e(u)}"> {e(u)}</label>'
                             for u in units)
        status = "ok" if policy.ok else f'<span class="warn">unavailable: {e(policy.error or "")}</span>'
        msg = f'<p class="msg">{e(message)}</p>' if message else ""
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Access console</title>
<link rel="stylesheet" href="/admin/admin.css"></head><body>
<h1>Access console</h1><p class="muted">Signed in as {e(user)} · knowledge {e(rt.knowledge.version)} · policy {status}</p>
{msg}
<h2>Grant units</h2>
<form method="post" action="/admin/grant"><input type="hidden" name="csrf" value="{token}">
<label>User <input type="text" name="user" required maxlength="320"></label>
<div>{unit_boxes}</div>
<label>Reason <input type="text" name="reason" required maxlength="{MAX_REASON}"></label>
<button>Grant now</button>
<p class="muted">Takes effect on the next request. Reconcile it into version control with scripts/reconcile_overlay.py.</p></form>
<h2>Revoke</h2>
<form method="post" action="/admin/revoke"><input type="hidden" name="csrf" value="{token}">
<label>User <input type="text" name="user" required maxlength="320"></label>
<label>Reason <input type="text" name="reason" required maxlength="{MAX_REASON}"></label>
<button>Revoke now</button>
<p class="muted">Denies the user everywhere, including grants in version control, until those are removed.</p></form>
<h2>Pending reconciliation</h2>
<table><tr><th>User</th><th>Action</th><th>Units</th><th>By</th><th>At</th><th>Reason</th></tr>{pending or '<tr><td colspan="6" class="muted">Nothing pending.</td></tr>'}</table>
<h2>Effective grants</h2>
<table><tr><th>User</th><th>Units</th><th>Ad hoc</th><th>Origin</th></tr>{grants}</table>
<h2>Decisions, last 24 hours</h2>
<table><tr><th>Tool</th><th>Outcome</th><th>Count</th></tr>{decisions}</table>
</body></html>"""

    # ---- form handling ---------------------------------------------------------------
    @staticmethod
    def parse_form(content_type: str, body: bytes) -> dict[str, list[str]] | None:
        if content_type.split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return None
        if len(body) > MAX_FORM_BYTES:
            return None
        try:
            return parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=64,
                            strict_parsing=False)
        except (UnicodeDecodeError, ValueError):
            return None

    def apply(self, action: str, admin: str, form: dict[str, list[str]]) -> str:
        """Validate and apply one console action; returns an outcome code from OUTCOMES."""
        user = (form.get("user") or [""])[0].strip().lower()
        reason = " ".join((form.get("reason") or [""])[0].split())
        if not USER_ID.match(user):
            return "invalid_user"
        if not reason or len(reason) > MAX_REASON:
            return "invalid_reason"
        if action == "grant":
            if user == admin:
                return "self_grant"
            units = sorted(set(form.get("units") or []))
            if not units or any(u not in self.rt.knowledge.units for u in units):
                return "invalid_units"
            return "granted" if T.admin_grant(self.rt, admin, user, units, reason) else "save_failed"
        return "revoked" if T.admin_revoke(self.rt, admin, user, reason) else "save_failed"


# Fixed outcome messages. The redirect carries only a code, so a crafted link cannot make
# the console display text of an attacker's choosing.
OUTCOMES = {
    "granted": "Grant saved. It applies from the next request; see Pending reconciliation.",
    "revoked": "Revocation saved. It applies from the next request.",
    "invalid_user": "Enter the user's identity as name@domain.",
    "invalid_reason": f"A reason is required (at most {MAX_REASON} characters).",
    "invalid_units": "Choose at least one known unit.",
    "save_failed": "The change could not be saved; nothing changed.",
    "self_grant": "Administrators cannot grant themselves access; ask another administrator to "
                  "make the change in version control.",
}


def register_admin(mcp: FastMCP, rt: Runtime) -> None:
    """Mount the console only when both secrets are configured (config.problems enforces both
    or neither); otherwise its routes do not exist."""
    if not (rt.settings.proxy_secret and rt.settings.csrf_secret):
        return
    from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

    console = Console(rt)

    def secured(response: Response) -> Response:
        response.headers.update(SECURITY_HEADERS)
        return response

    def refuse(headers: Any) -> Response:
        if not console.came_via_proxy(headers):
            return secured(PlainTextResponse("Not found", status_code=404))
        return secured(PlainTextResponse("Forbidden", status_code=403))

    @mcp.custom_route("/admin/admin.css", methods=["GET"])
    async def admin_css(request):
        return secured(Response(CSS, media_type="text/css"))

    @mcp.custom_route("/admin", methods=["GET"])
    async def admin_home(request):
        user = console.identify(request.headers)
        if user is None:
            return refuse(request.headers)
        message = OUTCOMES.get(request.query_params.get("r", ""))
        return secured(HTMLResponse(await anyio.to_thread.run_sync(console.page, user, message)))

    async def handle(request, action: str):
        user = console.identify(request.headers)
        if user is None:
            return refuse(request.headers)
        if not console.origin_ok(request.headers):
            return secured(PlainTextResponse("Cross-origin request refused", status_code=403))
        declared = request.headers.get("content-length", "")
        if declared and (not declared.isdigit() or int(declared) > MAX_FORM_BYTES):
            return secured(PlainTextResponse("Request too large", status_code=413))
        body = bytearray()
        async for chunk in request.stream():        # a chunked body has no Content-Length
            body += chunk
            if len(body) > MAX_FORM_BYTES:
                return secured(PlainTextResponse("Request too large", status_code=413))
        form = console.parse_form(request.headers.get("content-type", ""), bytes(body))
        if form is None or not console.csrf_ok(user, (form.get("csrf") or [""])[0]):
            return secured(PlainTextResponse("Invalid or expired form; reload the page", status_code=403))
        outcome = await anyio.to_thread.run_sync(console.apply, action, user, form)
        return secured(RedirectResponse(f"/admin?r={outcome}", status_code=303))

    @mcp.custom_route("/admin/grant", methods=["POST"])
    async def admin_grant(request):
        return await handle(request, "grant")

    @mcp.custom_route("/admin/revoke", methods=["POST"])
    async def admin_revoke(request):
        return await handle(request, "revoke")
