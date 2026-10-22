"""Account linking, tested over raw HTTP the way Alexa+ would drive it (authorization code + PKCE S256)."""
import base64, hashlib, json, os, secrets, socket, sys, threading, time, unittest, urllib.error, urllib.parse, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import uvicorn
from huntkit import Config
from addon.oauth import ACCESS_TTL_S, LeakHuntAuth
from addon.server import build_server
from addon.service import HuntService
from addon.store import Store

REDIRECT = "https://client.example/cb"


class Clock:
    def __init__(self): self.t = time.time()
    def __call__(self): return self.t


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None


OPENER = urllib.request.build_opener(NoRedirect)


def http(method, url, data=None, headers=None):
    body = None if data is None else (data if isinstance(data, bytes) else urllib.parse.urlencode(data).encode())
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        r = OPENER.open(req, timeout=10)
        return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def pkce():
    v = secrets.token_urlsafe(48)
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


class OAuthHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.clock = Clock()
        cls.store = Store()
        cls.svc = HuntService(cls.store, cls.clock, Config(), key=b"k", public_base_url=cls.base)
        cls.svc.ensure_demo_household("default", control=False)
        cls.svc.ensure_demo_household("other", sources={}, control=False)
        cls.oauth = LeakHuntAuth(cls.store, issuer_url=cls.base, client_id="alexa-test", client_secret="s3cret", redirect_uris=[REDIRECT],
                                 pairing={"DEMO-1234": "default", "OTHER-9999": "other"}, clock=cls.clock)
        app = build_server(cls.svc, oauth=cls.oauth).streamable_http_app(json_response=True)
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="warning"))
        cls.th = threading.Thread(target=cls.server.run, daemon=True); cls.th.start()
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/healthz", timeout=0.5); break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True; cls.th.join(timeout=5)

    # ---------------------------------------------------------------- helpers
    def authorize(self, challenge, *, state="st8", redirect=REDIRECT, resource=None, client="alexa-test", method="S256"):
        q = {"response_type": "code", "client_id": client, "redirect_uri": redirect, "code_challenge": challenge,
             "code_challenge_method": method, "state": state, "scope": "leakhunt"}
        if resource: q["resource"] = resource
        return http("GET", self.base + "/authorize?" + urllib.parse.urlencode(q))

    def link(self, code="DEMO-1234", verifier_challenge=None, resource=None):
        v, c = verifier_challenge or pkce()
        st, hd, _ = self.authorize(c, resource=resource)
        self.assertEqual(st, 302, hd)
        loc = hd["location"]
        self.assertTrue(loc.startswith(self.base + "/login?req="), loc)
        req = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["req"][0]
        st, hd, _ = http("POST", self.base + "/login", {"req": req, "code": code})
        return v, req, st, hd

    def tokens(self, verifier, code, **over):
        d = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": "alexa-test",
             "client_secret": "s3cret", "code_verifier": verifier}
        d.update(over)
        st, hd, body = http("POST", self.base + "/token", d)
        return st, json.loads(body)

    def full_login(self, code="DEMO-1234"):
        v, req, st, hd = self.link(code)
        self.assertEqual(st, 302)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(hd["location"]).query)
        self.assertEqual(q["state"], ["st8"]); self.assertEqual(q["iss"], [self.base])
        st, tok = self.tokens(v, q["code"][0])
        self.assertEqual(st, 200, tok)
        return tok

    def mcp(self, token, method, params=None, sid=None):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
        if token: h["Authorization"] = "Bearer " + token
        if sid: h["Mcp-Session-Id"] = sid
        return http("POST", self.base + "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(), h)

    def call_tool(self, token, name, args=None):
        st, hd, body = self.mcp(token, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        self.assertEqual(st, 200, body)
        sid = hd["mcp-session-id"]
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25",
             "Authorization": "Bearer " + token, "Mcp-Session-Id": sid}
        http("POST", self.base + "/mcp", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(), h)
        st, hd, body = http("POST", self.base + "/mcp", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": name, "arguments": args or {}}}).encode(), h)
        self.assertEqual(st, 200, body)
        return json.loads(body)["result"]

    # ---------------------------------------------------------------- discovery
    def test_protected_resource_and_server_metadata(self):
        st, _, body = http("GET", self.base + "/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(st, 200)
        prm = json.loads(body)
        self.assertEqual(prm["resource"].rstrip("/"), self.base + "/mcp")
        self.assertIn(self.base, [a.rstrip("/") for a in prm["authorization_servers"]])
        st, _, body = http("GET", self.base + "/.well-known/oauth-authorization-server")
        md = json.loads(body)
        self.assertEqual(md["code_challenge_methods_supported"], ["S256"])
        self.assertNotIn("registration_endpoint", md)                    # no dynamic client registration
        self.assertIn("authorization_code", md["grant_types_supported"])

    def test_dynamic_registration_is_not_offered(self):
        st, _, _ = http("POST", self.base + "/register", json.dumps({"redirect_uris": [REDIRECT]}).encode(), {"Content-Type": "application/json"})
        self.assertIn(st, (404, 405))

    def test_unauthenticated_mcp_is_401_with_pointer_to_metadata(self):
        st, hd, _ = self.mcp(None, "tools/list")
        self.assertEqual(st, 401)
        www = hd.get("www-authenticate", "")
        self.assertIn("resource_metadata", www)

    # ---------------------------------------------------------------- the flow
    def test_full_flow_and_tools_work_with_the_token(self):
        tok = self.full_login()
        r = self.call_tool(tok["access_token"], "water_status")
        self.assertFalse(r.get("isError"))
        self.assertEqual(r["structuredContent"]["household"], "Demo house (simulated)")

    def test_code_is_single_use_and_pkce_is_enforced(self):
        v, req, st, hd = self.link()
        code = urllib.parse.parse_qs(urllib.parse.urlparse(hd["location"]).query)["code"][0]
        st, body = self.tokens("wrong-" + v, code)
        self.assertEqual(st, 400)
        st, body = self.tokens(v, code)                                  # a failed attempt must not burn... or must it?
        # either outcome is acceptable for the retry, but the NEXT replay must always fail
        if st == 200:
            st2, body2 = self.tokens(v, code)
            self.assertEqual(st2, 400)

    def test_redirect_uri_must_match_exactly(self):
        v, c = pkce()
        st, hd, body = self.authorize(c, redirect="https://evil.example/cb")
        self.assertNotEqual(st, 302 if "evil.example" in hd.get("location", "") else -1)
        self.assertNotIn("evil.example", hd.get("location", ""))

    def test_pkce_is_required_and_plain_is_refused(self):
        v, c = pkce()
        for chal, method in (("", "S256"), ("short", "S256"), (c, "plain")):
            st, hd, _ = self.authorize(chal, method=method)
            self.assertNotIn("/login", hd.get("location", ""), (chal, method))
            self.assertIn("error=invalid_request", hd.get("location", ""), (chal, method))
        q = {"response_type": "code", "client_id": "alexa-test", "redirect_uri": REDIRECT, "state": "x"}      # no challenge at all
        st, hd, _ = http("GET", self.base + "/authorize?" + urllib.parse.urlencode(q))
        self.assertIn("error=invalid_request", hd.get("location", ""))

    def test_unknown_client_and_wrong_secret_fail(self):
        v, c = pkce()
        st, hd, _ = self.authorize(c, client="someone-else")
        self.assertNotIn("/login", hd.get("location", ""))
        v, req, st, hd = self.link()
        code = urllib.parse.parse_qs(urllib.parse.urlparse(hd["location"]).query)["code"][0]
        st, body = self.tokens(v, code, client_secret="nope")
        self.assertEqual(st, 401 if st == 401 else 400)
        self.assertNotIn("access_token", body)

    def test_wrong_resource_is_refused(self):
        v, c = pkce()
        st, hd, _ = self.authorize(c, resource="https://other.example/mcp")
        self.assertNotIn("/login", hd.get("location", ""))

    def test_pairing_code_lockout(self):
        v, c = pkce()
        st, hd, _ = self.authorize(c)
        req = urllib.parse.parse_qs(urllib.parse.urlparse(hd["location"]).query)["req"][0]
        codes = []
        for i in range(6):
            st, hd2, body = http("POST", self.base + "/login", {"req": req, "code": f"WRONG-{i}"})
            codes.append(st)
        self.assertEqual(codes[:5], [400] * 5)
        self.assertEqual(codes[5], 429)
        st, hd2, body = http("POST", self.base + "/login", {"req": req, "code": "DEMO-1234"})     # even the right code no longer works
        self.assertEqual(st, 400)

    def test_login_page_is_accessible_and_not_framable(self):
        v, c = pkce()
        st, hd, _ = self.authorize(c)
        st, hd2, body = http("GET", hd["location"])
        self.assertEqual(st, 200)
        self.assertIn("deny", hd2["x-frame-options"].lower()); self.assertIn("no-store", hd2["cache-control"])
        self.assertIn(b"<label", body); self.assertIn(b'lang="en"', body)
        self.assertIn(b"never your main safety valve", body)

    # ---------------------------------------------------------------- refresh, expiry, isolation
    def test_refresh_rotates_and_reuse_kills_the_family(self):
        tok = self.full_login()
        d = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": "alexa-test", "client_secret": "s3cret"}
        st, _, body = http("POST", self.base + "/token", d)
        self.assertEqual(st, 200)
        new = json.loads(body)
        self.assertNotEqual(new["refresh_token"], tok["refresh_token"])
        st, _, _ = self.mcp(tok["access_token"], "tools/list")
        self.assertEqual(st, 401)                                           # old access token died with the rotation
        st, _, body = http("POST", self.base + "/token", d)                 # replay of the old refresh token
        self.assertEqual(st, 400)
        st, _, _ = self.mcp(new["access_token"], "tools/list")              # ...and the whole family is revoked
        self.assertEqual(st, 401)

    def test_expired_access_token_is_rejected(self):
        tok = self.full_login()
        self.assertFalse(self.call_tool(tok["access_token"], "night_flow").get("isError"))
        self.clock.t += ACCESS_TTL_S + 5
        try:
            st, _, _ = self.mcp(tok["access_token"], "tools/list")
            self.assertEqual(st, 401)
        finally:
            self.clock.t -= ACCESS_TTL_S + 5

    def test_households_are_isolated(self):
        a = self.full_login("DEMO-1234")["access_token"]
        b = self.full_login("OTHER-9999")["access_token"]
        hunt = self.call_tool(a, "start_leak_hunt")["structuredContent"]["hunt_id"]
        r = self.call_tool(b, "report_step", {"kind": "quiet", "hunt_id": hunt})
        self.assertTrue(r["isError"])                                       # another household cannot touch this hunt
        r = self.call_tool(b, "hunt_status", {"hunt_id": hunt})
        self.assertTrue(r["isError"])
        r = self.call_tool(b, "evidence_packet", {"hunt_id": hunt})
        self.assertTrue(r["isError"])

    def test_tokens_are_stored_hashed(self):
        tok = self.full_login()
        rows = self.store.fetch("SELECT token_hash FROM oauth_token")
        raw = {tok["access_token"], tok["refresh_token"]}
        self.assertTrue(all(r["token_hash"] not in raw for r in rows))


if __name__ == "__main__":
    unittest.main()
