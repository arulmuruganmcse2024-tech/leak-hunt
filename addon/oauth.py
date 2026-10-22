"""Account linking for Alexa+: a small OAuth 2.1 authorization server (code + PKCE S256, no dynamic client
registration, rotating refresh tokens) in front of the MCP resource server.

The customer proves they own the household by entering the pairing code shown by their water monitor's app.
Tokens carry the household id as `subject`; every tool call is scoped to it (see server.build_server).

Secrets are stored hashed (SHA-256). Codes and tokens have 256 bits of entropy. Authorization codes are single
use; a replayed refresh token revokes its whole family.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
                                      TokenError)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .store import Store

CODE_TTL_S = 300
PENDING_TTL_S = 600
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 30 * 86400
MAX_PAIRING_ATTEMPTS = 5
DEFAULT_SCOPES = ["leakhunt"]


_CHALLENGE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class LeakHuntAuth:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider."""

    def __init__(self, store: Store, *, issuer_url: str, client_id: str, client_secret: str, redirect_uris: List[str],
                 pairing: Dict[str, str], clock: Callable[[], float] = time.time, resource_url: Optional[str] = None):
        self.store, self.issuer, self.clock = store, issuer_url.rstrip("/"), clock
        self.resource = resource_url or self.issuer + "/mcp"
        self.pairing = dict(pairing)                  # pairing code -> household id
        self._client = OAuthClientInformationFull(
            client_id=client_id, client_secret=client_secret, redirect_uris=[AnyUrl(u) for u in redirect_uris],
            token_endpoint_auth_method="client_secret_post", grant_types=["authorization_code", "refresh_token"],
            response_types=["code"], scope=" ".join(DEFAULT_SCOPES), client_name="Alexa+")

    # ------------------------------------------------------------------ clients (pre-registered only)
    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return self._client if hmac.compare_digest(client_id, self._client.client_id or "") else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("dynamic client registration is disabled")

    # ------------------------------------------------------------------ authorization endpoint
    def _canonical_resource(self, resource: Optional[str]) -> str:
        if resource is None:
            return self.resource
        r = resource.rstrip("/")
        if r in (self.issuer, self.resource.rstrip("/")):
            return self.resource
        raise AuthorizeError("invalid_target", "unknown resource")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not _CHALLENGE.match(params.code_challenge or ""):          # RFC 7636 §4.2: 43-128 unreserved characters
            raise AuthorizeError("invalid_request", "code_challenge must be a base64url S256 challenge")
        now = self.clock()
        self.store.execute("DELETE FROM oauth_pending WHERE created < ?", (now - PENDING_TTL_S,))
        req = secrets.token_urlsafe(32)
        scopes = [s for s in (params.scopes or DEFAULT_SCOPES) if s in DEFAULT_SCOPES] or list(DEFAULT_SCOPES)
        self.store.execute(
            "INSERT INTO oauth_pending(id,client_id,redirect_uri,explicit,state,challenge,scopes,resource,created) VALUES(?,?,?,?,?,?,?,?,?)",
            (req, client.client_id, str(params.redirect_uri), int(params.redirect_uri_provided_explicitly), params.state,
             params.code_challenge, json.dumps(scopes), self._canonical_resource(params.resource), now))
        return f"{self.issuer}/login?{urlencode({'req': req})}"

    # login page + submit (registered as plain routes by the server) ------------------------------
    def _pending(self, req: str) -> Optional[dict]:
        rows = self.store.fetch("SELECT * FROM oauth_pending WHERE id=? AND created>=?", (req, self.clock() - PENDING_TTL_S))
        return rows[0] if rows else None

    async def login_get(self, request: Request) -> Response:
        p = self._pending(request.query_params.get("req", ""))
        if not p:
            return _page("Link expired", "<p>This sign-in link has expired. Go back to Alexa and start linking again.</p>", status=400)
        return _page("Link Leak Hunt", _form(request.query_params["req"], None))

    async def login_post(self, request: Request) -> Response:
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        req = (form.get("req") or [""])[0]
        code = (form.get("code") or [""])[0].strip().upper()
        p = self._pending(req)
        if not p:
            return _page("Link expired", "<p>This sign-in link has expired. Go back to Alexa and start linking again.</p>", status=400)
        if p["attempts"] >= MAX_PAIRING_ATTEMPTS:
            self.store.execute("DELETE FROM oauth_pending WHERE id=?", (req,))
            return _page("Too many tries", "<p>Too many wrong codes. Go back to Alexa and start linking again.</p>", status=429)
        hid = None
        for known, household in self.pairing.items():            # constant-time compare against every code
            if hmac.compare_digest(code.encode(), known.upper().encode()):
                hid = household
        if hid is None:
            self.store.execute("UPDATE oauth_pending SET attempts = attempts + 1 WHERE id=?", (req,))
            return _page("Link Leak Hunt", _form(req, "That code isn't right. Check the code in your water monitor's app."), status=400)
        self.store.execute("DELETE FROM oauth_pending WHERE id=?", (req,))        # one use
        raw = secrets.token_urlsafe(32)
        self.store.execute(
            "INSERT INTO oauth_code(code_hash,client_id,scopes,expires,challenge,redirect_uri,explicit,resource,subject) VALUES(?,?,?,?,?,?,?,?,?)",
            (_h(raw), p["client_id"], p["scopes"], self.clock() + CODE_TTL_S, p["challenge"], p["redirect_uri"], p["explicit"],
             p["resource"], hid))
        u = urlparse(p["redirect_uri"])
        q = parse_qs(u.query)
        q["code"] = [raw]
        if p["state"]:
            q["state"] = [p["state"]]
        q["iss"] = [self.issuer]                                    # RFC 9207
        loc = urlunparse(u._replace(query=urlencode({k: v[0] for k, v in q.items()})))
        return RedirectResponse(loc, status_code=302, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ token endpoint
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> Optional[AuthorizationCode]:
        rows = self.store.fetch("SELECT * FROM oauth_code WHERE code_hash=?", (_h(authorization_code),))
        if not rows or rows[0]["client_id"] != client.client_id:
            return None
        r = rows[0]
        return AuthorizationCode(code=authorization_code, scopes=json.loads(r["scopes"]), expires_at=r["expires"], client_id=r["client_id"],
                                 code_challenge=r["challenge"], redirect_uri=AnyUrl(r["redirect_uri"]),
                                 redirect_uri_provided_explicitly=bool(r["explicit"]), resource=r["resource"], subject=r["subject"])

    def _issue(self, client_id: str, scopes: List[str], resource: Optional[str], subject: str, family: str) -> OAuthToken:
        now = self.clock()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sc = json.dumps(scopes)
        self.store.execute("INSERT INTO oauth_token(token_hash,kind,client_id,scopes,expires,resource,subject,family) VALUES(?,?,?,?,?,?,?,?)",
                           (_h(access), "access", client_id, sc, now + ACCESS_TTL_S, resource, subject, family))
        self.store.execute("INSERT INTO oauth_token(token_hash,kind,client_id,scopes,expires,resource,subject,family) VALUES(?,?,?,?,?,?,?,?)",
                           (_h(refresh), "refresh", client_id, sc, now + REFRESH_TTL_S, resource, subject, family))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL_S, scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        gone = self.store.execute("DELETE FROM oauth_code WHERE code_hash=?", (_h(authorization_code.code),))
        if not gone:                                               # raced / replayed
            raise TokenError("invalid_grant", "authorization code already used")
        if authorization_code.expires_at < self.clock():
            raise TokenError("invalid_grant", "authorization code expired")
        return self._issue(client.client_id or "", authorization_code.scopes, authorization_code.resource,
                           authorization_code.subject or "", secrets.token_hex(8))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[RefreshToken]:
        rows = self.store.fetch("SELECT * FROM oauth_token WHERE token_hash=? AND kind='refresh'", (_h(refresh_token),))
        if not rows or rows[0]["client_id"] != client.client_id:
            return None
        r = rows[0]
        if r["revoked"]:                                           # reuse of a rotated token: assume theft, kill the family
            self.store.execute("UPDATE oauth_token SET revoked=1 WHERE family=?", (r["family"],))
            return None
        if r["expires"] < self.clock():
            return None
        return RefreshToken(token=refresh_token, client_id=r["client_id"], scopes=json.loads(r["scopes"]), expires_at=int(r["expires"]),
                            resource=r["resource"], subject=r["subject"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: List[str]) -> OAuthToken:
        rows = self.store.fetch("SELECT family FROM oauth_token WHERE token_hash=?", (_h(refresh_token.token),))
        if not rows:
            raise TokenError("invalid_grant", "unknown refresh token")
        fam = rows[0]["family"]
        granted = [s for s in (scopes or refresh_token.scopes) if s in refresh_token.scopes]
        if not granted:
            raise TokenError("invalid_scope", "scope not granted")
        self.store.execute("UPDATE oauth_token SET revoked=1 WHERE token_hash=?", (_h(refresh_token.token),))       # rotate
        self.store.execute("UPDATE oauth_token SET revoked=1 WHERE family=? AND kind='access'", (fam,))
        return self._issue(client.client_id or "", granted, refresh_token.resource, refresh_token.subject or "", fam)

    # ------------------------------------------------------------------ resource server side
    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        rows = self.store.fetch("SELECT * FROM oauth_token WHERE token_hash=? AND kind='access'", (_h(token),))
        if not rows:
            return None
        r = rows[0]
        if r["revoked"] or r["expires"] < self.clock():
            return None
        return AccessToken(token=token, client_id=r["client_id"], scopes=json.loads(r["scopes"]), expires_at=int(r["expires"]),
                           resource=r["resource"], subject=r["subject"], claims={"iss": self.issuer})

    async def revoke_token(self, token) -> None:
        rows = self.store.fetch("SELECT family FROM oauth_token WHERE token_hash=?", (_h(token.token),))
        if rows:
            self.store.execute("UPDATE oauth_token SET revoked=1 WHERE family=?", (rows[0]["family"],))


# ---------------------------------------------------------------------------------- tiny accessible login page
def _form(req: str, error: Optional[str]) -> str:
    e = f"<p class='err' role='alert'>{html.escape(error)}</p>" if error else ""
    return (f"<p>Enter the pairing code from your water monitor's app to let Alexa show your water data and guide a leak hunt. "
            f"Alexa can switch only the pipes your pipe controller reports, never your main safety valve, and only after you agree.</p>{e}"
            f"<form method='post' action='/login'><input type='hidden' name='req' value='{html.escape(req)}'>"
            f"<label for='code'>Pairing code</label>"
            f"<input id='code' name='code' autocomplete='one-time-code' autocapitalize='characters' autofocus required maxlength='40'>"
            f"<button type='submit'>Link my home</button></form>")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{color-scheme:light dark;--bg:#fff;--ink:#14181e;--acc:#00607f;--bad:#b3261e;--line:#c9ced6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14181e;--ink:#f2f4f7;--acc:#4fd6ff;--bad:#ff8a80;--line:#3a4350}}}}
body{{background:var(--bg);color:var(--ink);font:18px/1.5 system-ui,sans-serif;max-width:30rem;margin:0 auto;padding:1.5rem 1rem}}
h1{{font-size:1.4rem}}label{{display:block;font-weight:700;margin:1rem 0 .3rem}}
input{{width:100%;box-sizing:border-box;font:inherit;padding:.7rem;border:2px solid var(--line);border-radius:10px;background:transparent;color:inherit;min-height:48px}}
button{{margin-top:1rem;min-height:56px;padding:0 1.4rem;font:inherit;font-weight:700;border:0;border-radius:14px;background:var(--acc);color:var(--bg);cursor:pointer}}
:focus-visible{{outline:3px solid var(--acc);outline-offset:3px}}.err{{color:var(--bad);font-weight:700}}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""
    return HTMLResponse(doc, status_code=status, headers={
        "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})
