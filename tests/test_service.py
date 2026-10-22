import json, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from huntkit import Config
from addon.service import HuntService, UserError
from addon.store import Store

T0 = 1_800_000_000.0 + 23 * 3600          # a fixed 'evening'


class FakeClock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def make(sources=None, control=False, **kw):
    clock = FakeClock()
    svc = HuntService(Store(), clock, Config(), key=b"test-key", public_base_url="https://x.test")
    svc.ensure_demo_household("h", sources=sources if sources is not None else {"toilet": 2.0}, control=control, **kw)
    return svc, clock


def autopilot(svc, clock, hid="h", hunt_id=None, max_s=900, step_delay=3.0):
    v = svc.start_hunt(hid) if hunt_id is None else svc.hunt_status(hid, hunt_id)
    hunt_id = v["hunt_id"]
    t_end = clock() + max_s
    while clock() < t_end:
        v = svc.hunt_status(hid, hunt_id)
        a = v["action"]
        if v["status"] != "active":
            return v
        if a["kind"] == "wait":
            clock.advance(2)
        elif a["kind"] == "quiet":
            clock.advance(step_delay); svc.report(hid, hunt_id, "quiet")
        elif a["kind"] in ("close", "open"):
            clock.advance(step_delay); svc.report(hid, hunt_id, a["kind"], (a["close"] or a["open"])[0])
        elif a["kind"] == "set_state":
            clock.advance(step_delay)
            for n in a["close"]: svc.report(hid, hunt_id, "close", n)
            for n in a["open"]: svc.report(hid, hunt_id, "open", n)
    return svc.hunt_status(hid, hunt_id)


class ServiceFlow(unittest.TestCase):
    def test_status_flags_the_overnight_flow_and_labels_simulation(self):
        svc, clock = make()
        s = svc.water_status("h")
        self.assertTrue(s["simulated"])
        self.assertIn(s["night"]["verdict"], ("new_overnight_flow", "ongoing_overnight_flow"))
        self.assertTrue(s["suggest_hunt"])
        self.assertIn("flowing", s["speech"])

    def test_clean_house_status_does_not_overclaim(self):
        svc, clock = make(sources={}, leak_nights=0)
        s = svc.water_status("h")
        self.assertEqual(s["night"]["verdict"], "normal")
        self.assertNotIn("no leak", s["speech"].lower())
        self.assertFalse(s["suggest_hunt"])

    def test_full_hunt_over_the_service_finds_the_toilet_and_proves_it_alone(self):
        svc, clock = make()
        v = autopilot(svc, clock)
        self.assertEqual(v["status"], "done")
        self.assertEqual(v["outcome"], "only_sources")
        self.assertEqual(v["found"], ["toilet"])
        self.assertIn("closed until it's repaired", v["speech"])
        self.assertTrue(v["simulated"])

    def test_hunt_resumes_across_sessions_and_restart(self):
        svc, clock = make()
        v = svc.start_hunt("h")
        hid = v["hunt_id"]
        clock.advance(5); svc.report("h", hid, "quiet")
        clock.advance(40)
        # a "new conversation": new service object over the same store
        svc2 = HuntService(svc.store, clock, Config(), key=b"test-key")
        again = svc2.start_hunt("h")
        self.assertTrue(again["resumed"])
        self.assertEqual(again["hunt_id"], hid)
        done = autopilot(svc2, clock, hunt_id=hid)
        self.assertEqual(done["outcome"], "only_sources")

    def test_abandoned_hunt_is_not_resumed(self):
        svc, clock = make()
        v = svc.start_hunt("h")
        clock.advance(2 * 3600)
        again = svc.start_hunt("h")
        self.assertFalse(again["resumed"])
        self.assertNotEqual(again["hunt_id"], v["hunt_id"])

    def test_duplicate_report_is_ignored(self):
        svc, clock = make()
        v = svc.start_hunt("h"); hid = v["hunt_id"]
        clock.advance(4); svc.report("h", hid, "quiet")
        clock.advance(1); out = svc.report("h", hid, "quiet")
        self.assertIn("Duplicate", out.get("ignored", ""))
        self.assertEqual(len(svc.store.reports(hid)), 1)

    def test_bad_inputs_are_user_errors_with_speakable_messages(self):
        svc, clock = make()
        hid = svc.start_hunt("h")["hunt_id"]
        with self.assertRaises(UserError) as e:
            svc.report("h", hid, "close", "jacuzzi")
        self.assertIn("I have:", str(e.exception))
        with self.assertRaises(UserError):
            svc.report("h", hid, "close", None)
        with self.assertRaises(UserError):
            svc.report("h", "h_nope", "quiet")
        with self.assertRaises(UserError):
            svc.report("h", hid, "undo")

    def test_suspect_boosts_prior_for_this_hunt_only(self):
        svc, clock = make(sources={"garden": 2.0})
        v = svc.start_hunt("h", suspect=["garden tap"])
        hunt = svc.store.hunt(v["hunt_id"])
        pri = {n["id"]: n["prior"] for n in hunt["topology"]}
        self.assertEqual(pri["garden"], 3.0)
        self.assertEqual({n["id"]: n["prior"] for n in svc.household("h")["topology"]}["garden"], 1.0)
        done = autopilot(svc, clock, hunt_id=v["hunt_id"])
        self.assertEqual(done["found"], ["garden"])

    def test_ambiguous_suspect_name_asks(self):
        svc, clock = make()
        with self.assertRaises(UserError) as e:
            svc.start_hunt("h", suspect=["kitchen"])      # kitchen pipe 1 or kitchen pipe 2?
        self.assertIn("kitchen pipe 1 or kitchen pipe 2", str(e.exception))
        svc.start_hunt("h", suspect=["bathroom"])         # the bathroom branch valve covers toilet and shower: not ambiguous


class Evidence(unittest.TestCase):
    def _done(self):
        svc, clock = make()
        v = autopilot(svc, clock)
        return svc, v

    def test_packet_is_signed_and_replays_to_the_same_result(self):
        svc, v = self._done()
        p = svc.evidence("h", v["hunt_id"])
        r = svc.verify_packet(p)
        self.assertEqual(r, dict(hash_ok=True, hmac_ok=True, replay_matches=True))
        self.assertTrue(p["simulated"])

    def test_tampering_is_detected(self):
        svc, v = self._done()
        p = svc.evidence("h", v["hunt_id"])
        p2 = json.loads(json.dumps(p))
        p2["readings"][-1]["total_l"] += 1.0
        r = svc.verify_packet(p2)
        self.assertFalse(r["hash_ok"])
        self.assertFalse(r["replay_matches"] and r["hash_ok"])
        # an attacker who recomputes the hash still cannot forge the HMAC without the key
        import hashlib
        from addon.service import _canon
        body = {k: x for k, x in p2.items() if k not in ("sha256", "hmac_sha256")}
        p2["sha256"] = hashlib.sha256(_canon(body)).hexdigest()
        r = svc.verify_packet(p2)
        self.assertTrue(r["hash_ok"]); self.assertFalse(r["hmac_ok"])

    def test_unfinished_hunt_has_no_evidence(self):
        svc, clock = make()
        hid = svc.start_hunt("h")["hunt_id"]
        with self.assertRaises(UserError):
            svc.evidence("h", hid)

    def test_html_is_self_contained_and_labels_simulation(self):
        svc, v = self._done()
        html = svc.evidence_html(svc.evidence("h", v["hunt_id"]))
        self.assertIn("SIMULATED HOUSE", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertNotIn("<script", html)


class Safety(unittest.TestCase):
    """Where water can be moved from is a short, auditable list."""

    def _src(self, fn):
        path = os.path.join(os.path.dirname(__file__), "..", "addon", fn)
        if fn == "sensor_adapter.py" and not os.path.exists(path):
            return ""                                              # the optional adapter is not in this build
        with open(path) as fh:
            return fh.read()

    def test_only_the_actuator_module_can_write_to_the_network(self):
        base = os.path.join(os.path.dirname(__file__), "..", "addon")
        for fn in os.listdir(base):
            if fn.endswith(".py") and fn != "actuators.py":
                src = self._src(fn).lower()
                for bad in ("method=\"post\"", "method=\"put\"", "method='put'", "requests.post", "requests.put",
                            "urlopen(req, data", "data=data"):
                    self.assertNotIn(bad, src, (fn, bad))
                if fn != "oauth.py":
                    self.assertNotIn("method='post'", src, fn)       # oauth.py only has an HTML <form method='post'>

    def test_the_telemetry_source_is_read_only_and_no_code_addresses_the_safety_valve(self):
        for fn in ("sources.py", "sensor_adapter.py"):
            src = self._src(fn)
            self.assertNotIn("PUT", src, fn)
            self.assertNotIn("POST", src, fn)
        for fn in ("actuators.py", "service.py", "server.py", "sources.py", "sensor_adapter.py"):
            low = self._src(fn).lower()
            self.assertNotIn("/valve", low, fn)
            self.assertNotIn("valve_command", low, fn)
            self.assertNotIn("rearm", low.replace("re-arm", ""), fn)

    def test_every_valve_command_goes_through_one_audited_function(self):
        src = self._src("service.py")
        self.assertEqual(src.count("hub.command("), 1)

    def test_control_is_off_unless_the_operator_enabled_a_hub(self):
        svc, clock = make(control=False)
        self.assertFalse(svc.control_available("h"))
        with self.assertRaises(UserError):
            svc.set_pipe("h", "toilet", "off")
        v = svc.start_hunt("h", mode="auto", consent=True)       # asked for auto, no hub: guided, and it says so
        self.assertEqual(v["mode"], "guided")
        self.assertIn("guide you instead", v["speech"])


if __name__ == "__main__":
    unittest.main()
