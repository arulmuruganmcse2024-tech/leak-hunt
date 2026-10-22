"""Talk to the real MCP server over raw HTTP JSON-RPC, the way a host (Alexa+) would."""
import json, os, socket, sys, threading, time, unittest, urllib.error, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import uvicorn
from huntkit import Config
from addon.server import UI_URI, build_server
from addon.service import HuntService
from addon.store import Store

T0 = 1_800_000_000.0 + 23 * 3600


class FakeClock:
    def __init__(self): self.t = T0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Client:
    def __init__(self, base):
        self.base, self.sid, self.n = base, None, 0

    def raw(self, path, body=None, headers=None, method="POST"):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
        if self.sid: h["Mcp-Session-Id"] = self.sid
        h["MCP-Protocol-Version"] = "2025-11-25"
        req = urllib.request.Request(self.base + path, data=None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode()),
                                     headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def rpc(self, method, params=None):
        self.n += 1
        st, hd, body = self.raw("/mcp", {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        if "mcp-session-id" in {k.lower() for k in hd}:
            self.sid = {k.lower(): v for k, v in hd.items()}["mcp-session-id"]
        return st, json.loads(body) if body else None

    def notify(self, method, params=None):
        return self.raw("/mcp", {"jsonrpc": "2.0", "method": method, "params": params or {}})

    def init(self):
        st, r = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {
            "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}},
            "clientInfo": {"name": "test-host", "version": "0"}})
        self.notify("notifications/initialized")
        return r

    def call(self, name, args=None):
        st, r = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        return r


class ServerHTTP(unittest.TestCase):
    CONTROL = False                     # False: no pipe hub, the original guided-only product

    @classmethod
    def setUpClass(cls):
        cls.clock = FakeClock()
        cls.svc = HuntService(Store(), cls.clock, Config(), key=b"k", public_base_url="http://127.0.0.1")
        cls.svc.ensure_demo_household("default", control=cls.CONTROL)
        cls.mcp = build_server(cls.svc)
        app = cls.mcp.streamable_http_app(json_response=True)
        port = free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        cls.th = threading.Thread(target=cls.server.run, daemon=True); cls.th.start()
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/healthz", timeout=0.5); break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True; cls.th.join(timeout=5)

    def client(self):
        c = Client(self.base); c.init(); return c

    def test_initialize_advertises_tools_and_apps_extension(self):
        c = Client(self.base); r = c.init()
        res = r["result"]
        self.assertEqual(res["serverInfo"]["name"], "Leak Hunt")
        self.assertIn("tools", res["capabilities"])
        # The 2025-11-25 handshake has no `extensions` capability field; hosts discover the UI from each tool's
        # `_meta.ui.resourceUri` (checked in test_tools_list_shape). The SDK registers the extension itself:
        self.assertIn("io.modelcontextprotocol/ui", self.__class__.mcp._lowlevel_server.extensions)
        self.assertIn("Never claim there is no leak", res["instructions"])

    def test_tools_list_shape(self):
        c = self.client()
        _, r = c.rpc("tools/list")
        tools = {t["name"]: t for t in r["result"]["tools"]}
        self.assertEqual(set(tools), {"water_status", "night_flow", "start_leak_hunt", "report_step", "hunt_status", "evidence_packet",
                                      "set_household", "stop_leak_hunt", "list_pipes", "set_pipe", "fill_water", "stop_filling", "fill_status",
                                      "house_mode"})
        for n in ("water_status", "start_leak_hunt", "report_step", "hunt_status", "stop_leak_hunt", "list_pipes", "set_pipe",
                  "fill_water", "stop_filling", "fill_status", "house_mode"):
            ui = tools[n]["_meta"]["ui"]
            self.assertEqual(ui["resourceUri"], UI_URI)
            self.assertEqual(set(ui["visibility"]), {"model", "app"})
        for t in tools.values():
            self.assertTrue(t["description"]); self.assertEqual(t["inputSchema"]["type"], "object")
        self.assertTrue(tools["water_status"]["annotations"]["readOnlyHint"])
        self.assertEqual(tools["report_step"]["inputSchema"]["properties"]["kind"]["enum"], ["quiet", "close", "open", "undo"])
        self.assertEqual(tools["set_pipe"]["inputSchema"]["properties"]["state"]["enum"], ["on", "off"])
        sp = tools["set_pipe"]["annotations"]
        self.assertFalse(sp["readOnlyHint"]); self.assertFalse(sp["destructiveHint"]); self.assertTrue(sp["idempotentHint"])
        self.assertTrue(tools["list_pipes"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["fill_status"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["fill_water"]["annotations"]["readOnlyHint"])
        self.assertIn("temperature", tools["fill_water"]["description"])
        self.assertEqual(set(tools["start_leak_hunt"]["inputSchema"]["properties"]["mode"]["anyOf"][0]["enum"]), {"auto", "guided"})

    def test_ui_resource_is_served_as_mcp_app_with_closed_csp(self):
        c = self.client()
        _, r = c.rpc("resources/read", {"uri": UI_URI})
        item = r["result"]["contents"][0]
        self.assertEqual(item["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("ui/initialize", item["text"])
        csp = (item.get("_meta") or {}).get("ui", {}).get("csp", {})
        self.assertFalse(csp.get("connectDomains")); self.assertFalse(csp.get("resourceDomains"))
        self.assertNotIn("http://", item["text"].replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", item["text"])
        self.assertNotIn("WebSocket", item["text"]); self.assertNotIn("EventSource", item["text"])

    def test_full_hunt_over_http_with_voice_screen_parity(self):
        c = self.client()
        st = c.call("water_status")["result"]
        self.assertFalse(st.get("isError"))
        self.assertEqual(st["content"][0]["text"], st["structuredContent"]["speech"])
        self.assertTrue(st["structuredContent"]["simulated"])
        r = c.call("start_leak_hunt")["result"]
        self.assertEqual(r["structuredContent"]["kind"], "hunt")           # no pipe hub: straight to a guided hunt
        hid = r["structuredContent"]["hunt_id"]
        guard = 0
        while guard < 400:
            guard += 1
            sc = r["structuredContent"]
            self.assertEqual(r["content"][0]["text"], sc["speech"])         # what is said == what is shown
            self.assertEqual(r["_meta"]["ui"]["resourceUri"], UI_URI) if "_meta" in r else None
            if sc["status"] != "active": break
            a = sc["action"]
            if a["kind"] == "wait":
                self.clock.advance(2); r = c.call("hunt_status", {"hunt_id": hid})["result"]
            elif a["kind"] == "quiet":
                self.clock.advance(3); r = c.call("report_step", {"kind": "quiet", "hunt_id": hid})["result"]
            elif a["kind"] in ("close", "open"):
                self.clock.advance(3); r = c.call("report_step", {"kind": a["kind"], "valve": (a["close"] or a["open"])[0], "hunt_id": hid})["result"]
            else:
                for n in a["close"]: r = c.call("report_step", {"kind": "close", "valve": n, "hunt_id": hid})["result"]
                for n in a["open"]: r = c.call("report_step", {"kind": "open", "valve": n, "hunt_id": hid})["result"]
        sc = r["structuredContent"]
        self.assertEqual(sc["status"], "done"); self.assertEqual(sc["outcome"], "only_sources"); self.assertEqual(sc["found"], ["toilet"])
        ev = c.call("evidence_packet", {"hunt_id": hid})["result"]["structuredContent"]
        self.assertEqual(len(ev["sha256"]), 64); self.assertIn("/evidence/", ev["url"])
        # the link works with its token and not without
        path = ev["url"].replace("http://127.0.0.1", "")
        st, _, body = c.raw(path, method="GET", body=None)
        self.assertEqual(st, 200); self.assertIn(b"SIMULATED HOUSE", body)
        st, _, _ = c.raw(path.split("?")[0] + "?k=wrong", method="GET")
        self.assertEqual(st, 404)
        st, _, jb = c.raw(path.split("?")[0] + ".json?" + path.split("?")[1], method="GET")
        self.assertEqual(json.loads(jb)["sha256"], ev["sha256"])

    def test_errors_are_tool_errors_not_crashes(self):
        c = self.client()
        r = c.call("report_step", {"kind": "close", "valve": "toilet", "hunt_id": "h_nope"})["result"]
        self.assertTrue(r["isError"]); self.assertIn("leak hunt", r["content"][0]["text"])
        _, bad = c.rpc("tools/call", {"name": "does_not_exist", "arguments": {}})
        self.assertTrue("error" in bad or bad["result"].get("isError"))
        r = c.call("report_step", {"kind": "explode"})
        self.assertTrue("error" in r or r["result"].get("isError"))

    def test_no_tool_can_address_the_safety_valve(self):
        c = self.client()
        _, r = c.rpc("tools/list")
        for t in r["result"]["tools"]:
            blob = (t["name"] + " " + t["description"]).lower()
            for w in ("open the safety", "close the safety", "re-arm the", "rearm", "relay"):
                self.assertNotIn(w, blob)
        self.assertIn("never works on the main safety valve", {t["name"]: t["description"].lower() for t in r["result"]["tools"]}["set_pipe"])
        for name in ("safety valve", "main valve", "the main", "inlet"):
            res = c.call("set_pipe", {"pipe": name, "state": "off"})["result"]
            self.assertTrue(res["isError"], name)                         # there is no such pipe to switch

    def test_a_tool_cannot_turn_control_on(self):
        c = self.client()
        res = c.call("set_pipe", {"pipe": "toilet", "state": "off"})["result"]
        self.assertTrue(res["isError"]); self.assertIn("can't switch pipes", res["content"][0]["text"])
        c.call("set_household", {"valves": [{"room": "kitchen"}, {"room": "kitchen"}, {"label": "garden tap"}]})
        self.assertFalse(self.svc.control_available("default"))
        c.call("set_household", {"valves": [{"room": "kitchen"}, {"room": "kitchen"}]})   # restore a layout for later tests
        self.svc.set_household("default", "Demo house (simulated)", __import__("huntkit").demo_topology().to_json())

    def test_set_household_numbers_the_pipes_in_a_room(self):
        c = self.client()
        r = c.call("set_household", {"valves": [{"room": "kitchen"}, {"room": "kitchen"}, {"room": "bathroom", "name": "toilet"}]})["result"]
        self.assertIn("kitchen pipe 1, kitchen pipe 2, bathroom toilet", r["content"][0]["text"])
        self.svc.set_household("default", "Demo house (simulated)", __import__("huntkit").demo_topology().to_json())

    def test_health(self):
        st, _, b = Client(self.base).raw("/healthz", method="GET")
        self.assertEqual(st, 200)


class ServerHTTPWithPipeHub(ServerHTTP):
    """The same server with a (simulated) pipe hub: voice pipe control and the automatic hunt, over real HTTP."""
    CONTROL = True

    def _physical(self):
        return sorted(self.svc._source("default").physical_closed())

    def test_full_hunt_over_http_with_voice_screen_parity(self):
        pass                                    # the guided flow is covered by the base class; here start_leak_hunt asks first

    def test_a_tool_cannot_turn_control_on(self):
        pass

    def test_set_household_numbers_the_pipes_in_a_room(self):
        pass

    def test_voice_flow_for_two_kitchen_pipes(self):
        c = self.client()
        r = c.call("list_pipes")["result"]
        self.assertEqual(r["content"][0]["text"], r["structuredContent"]["speech"])
        self.assertIn("kitchen pipe 1", r["content"][0]["text"])
        self.assertEqual(r["_meta"]["ui"]["resourceUri"], UI_URI)
        r = c.call("set_pipe", {"pipe": "kitchen", "state": "on"})["result"]            # which of the two?
        sc = r["structuredContent"]
        self.assertEqual(sc["kind"], "choose_pipe")
        self.assertIn("kitchen pipe 1 or kitchen pipe 2", r["content"][0]["text"])
        self.assertFalse(r.get("isError"))
        self.clock.advance(3)
        r = c.call("set_pipe", {"pipe": "kitchen pipe 2", "state": "off"})["result"]
        self.assertEqual(r["content"][0]["text"], "Kitchen pipe 2 is off.")
        self.assertEqual(self._physical(), ["kitchen2"])
        self.clock.advance(3)
        c.call("set_pipe", {"pipe": "kitchen", "state": "on", "all_in_room": True})
        self.assertEqual(self._physical(), [])

    def test_automatic_hunt_over_http(self):
        c = self.client()
        self.clock.advance(60)
        r = c.call("start_leak_hunt")["result"]
        self.assertEqual(r["structuredContent"]["kind"], "choose_mode")
        self.assertEqual(r["content"][0]["text"], r["structuredContent"]["speech"])
        r = c.call("start_leak_hunt", {"mode": "auto"})["result"]
        self.assertEqual(r["structuredContent"]["kind"], "needs_consent")
        self.assertEqual(self._physical(), [])
        r = c.call("start_leak_hunt", {"mode": "auto", "consent": True})["result"]
        sc = r["structuredContent"]; hid = sc["hunt_id"]
        self.assertEqual(sc["mode"], "auto")
        self.clock.advance(3)
        r = c.call("report_step", {"kind": "quiet", "hunt_id": hid})["result"]
        for _ in range(400):
            self.clock.advance(1)
            self.svc.tick()
            r = c.call("hunt_status", {"hunt_id": hid})["result"]
            self.assertEqual(r["content"][0]["text"], r["structuredContent"]["speech"])
            if r["structuredContent"]["status"] != "active":
                break
        sc = r["structuredContent"]
        self.assertEqual((sc["status"], sc["outcome"], sc["found"]), ("done", "only_sources", ["toilet"]))
        self.assertEqual(self._physical(), ["toilet"])                                  # held on a lease, not yet kept
        self.assertIn("I'm holding the toilet off for up to two minutes", sc["speech"])
        self.assertEqual(self.svc.store.latches("default"), [])
        self.clock.advance(3)
        r = c.call("set_pipe", {"pipe": "toilet", "state": "off"})["result"]            # the customer says yes
        self.assertIn("I'll keep the toilet off until you tell me it's been repaired", r["content"][0]["text"])
        self.assertEqual([l["pipe"] for l in self.svc.store.latches("default")], ["toilet"])
        # the finished hunt can be written up, valve commands included
        ev = c.call("evidence_packet", {"hunt_id": hid})["result"]
        self.assertFalse(ev.get("isError"))
        pk = self.svc.evidence("default", hid)
        self.assertTrue(pk["actuations"])
        # reopening the repaired pipe needs a yes
        self.clock.advance(5)
        r = c.call("set_pipe", {"pipe": "toilet", "state": "on"})["result"]
        self.assertEqual(r["structuredContent"]["kind"], "needs_confirm")
        self.assertEqual(self._physical(), ["toilet"])
        r = c.call("set_pipe", {"pipe": "toilet", "state": "on", "confirm": True})["result"]
        self.assertEqual(self._physical(), [])

    def test_stop_tool_puts_everything_back(self):
        c = self.client()
        self.clock.advance(60)
        r = c.call("start_leak_hunt", {"mode": "auto", "consent": True, "resume": False})["result"]
        hid = r["structuredContent"]["hunt_id"]
        self.clock.advance(3)
        c.call("report_step", {"kind": "quiet", "hunt_id": hid})
        for _ in range(300):
            self.clock.advance(1); self.svc.tick()
            if self._physical():
                break
        self.assertTrue(self._physical())
        r = c.call("stop_leak_hunt")["result"]
        self.assertEqual(r["structuredContent"]["status"], "stopped")
        self.assertEqual(self._physical(), [])


class ServerHTTPPower(unittest.TestCase):
    """Power Hunt through the same server and the same tools, with utility="power" (simulated meter and plugs)."""

    @classmethod
    def setUpClass(cls):
        from addon.power import PowerService
        cls.clock = FakeClock()
        cls.svc = HuntService(Store(), cls.clock, Config(), key=b"k", public_base_url="http://127.0.0.1")
        cls.svc.ensure_demo_household("default", control=True)
        cls.svc.power = PowerService(Store(), cls.clock, key=b"k", public_base_url="http://127.0.0.1")
        cls.svc.power.ensure_demo_household("default")
        cls.mcp = build_server(cls.svc)
        port = free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = uvicorn.Server(uvicorn.Config(cls.mcp.streamable_http_app(json_response=True), host="127.0.0.1", port=port, log_level="warning"))
        cls.th = threading.Thread(target=cls.server.run, daemon=True); cls.th.start()
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/healthz", timeout=0.5); break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True; cls.th.join(timeout=5)

    def tick(self, n=1):
        for _ in range(n):
            self.clock.advance(1); self.svc.tick(); self.svc.power.tick()

    def test_the_same_tools_take_utility_and_water_stays_the_default(self):
        from addon import vocab
        c = Client(self.base); c.init()
        _, r = c.rpc("tools/list")
        tools = {t["name"]: t for t in r["result"]["tools"]}
        for n in ("water_status", "start_leak_hunt", "stop_leak_hunt", "list_pipes", "set_pipe", "report_step", "hunt_status",
                  "night_flow", "evidence_packet"):
            self.assertIn("utility", tools[n]["inputSchema"]["properties"], n)
        self.assertNotIn("utility", tools["fill_water"]["inputSchema"]["properties"])
        w = c.call("water_status")["result"]["structuredContent"]
        self.assertNotIn("utility", w)
        self.assertIn("litres per minute", w["speech"])
        p = c.call("water_status", {"utility": "power"})["result"]
        self.assertEqual(p["structuredContent"]["utility"], "power")
        self.assertIn("never drew less than 108 watts", p["content"][0]["text"])
        self.assertEqual(vocab.water_words(p["structuredContent"]), [])
        _, res = c.rpc("resources/read", {"uri": UI_URI})
        page = res["result"]["contents"][0]["text"]
        self.assertNotIn("[/*POWER_RULES*/]", page)
        self.assertIn("litres per minute", page)                    # the rules travel with the screen

    def test_a_power_hunt_over_http(self):
        import re
        from addon import vocab
        c = Client(self.base); c.init()
        r = c.call("start_leak_hunt", {"utility": "power", "mode": "auto", "consent": True, "resume": False})["result"]["structuredContent"]
        hid = r["hunt_id"]
        self.assertTrue(hid.startswith("p_"))
        self.clock.advance(3)
        c.call("report_step", {"kind": "quiet", "hunt_id": hid})              # found by its id, no utility needed
        for _ in range(400):
            self.tick()
            st = c.call("hunt_status")["result"]["structuredContent"]       # no id, no utility: the hunt that is running
            if st["status"] != "active":
                break
        self.assertEqual((st["status"], st["found"], st["utility"]), ("done", ["tv"], "power"))
        self.assertEqual(vocab.water_words(st), [])
        self.clock.advance(4)
        k = c.call("set_pipe", {"pipe": "TV plug", "state": "off"})["result"]["structuredContent"]   # "plug" routes to power
        self.assertEqual(k["kept"], ["tv"])
        ev = c.call("evidence_packet", {"hunt_id": hid})["result"]["structuredContent"]
        self.assertIn("electrician", ev["speech"])
        url = "/evidence/" + hid + "?k=" + self.svc.power.evidence_token(hid)
        st_, _, body = c.raw(url, method="GET")
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", body.decode(), flags=re.S))
        self.assertEqual((st_, vocab.WATER_WORDS.findall(text)), (200, []))
        self.clock.advance(5)
        c.call("set_pipe", {"pipe": "TV plug", "state": "on", "confirm": True})

    def test_stop_with_no_detail_stops_a_power_hunt(self):
        c = Client(self.base); c.init()
        self.clock.advance(5)
        r = c.call("start_leak_hunt", {"utility": "power", "mode": "auto", "consent": True, "resume": False})["result"]["structuredContent"]
        self.clock.advance(3)
        c.call("report_step", {"kind": "quiet", "hunt_id": r["hunt_id"]})
        hub = self.svc.power._hub("default")
        for _ in range(200):
            self.tick()
            if any(p.position == "closed" for p in hub.pipes(self.clock()).values()):
                break
        s = c.call("stop_leak_hunt")["result"]["structuredContent"]
        self.assertEqual((s["status"], s["utility"]), ("stopped", "power"))
        self.assertTrue(all(p.position == "open" for p in hub.pipes(self.clock()).values()))


if __name__ == "__main__":
    unittest.main()
