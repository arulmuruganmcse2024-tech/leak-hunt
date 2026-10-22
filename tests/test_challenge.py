"""Challenge mode ("Try to fool it"): a hidden leak, chaos thrown at a live hunt, and a reveal.

All of it is simulation: the simulated house, the simulated pipe controller and a fake clock. What these tests pin is the
promise the mode makes to a judge: whatever is thrown at it, Leak Hunt either names the right valve or declines, and one
outside flow change never produces a false accusation.
"""
import json, os, sys, threading, time, unittest, urllib.error, urllib.request, warnings
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import uvicorn
from addon.challenge import Challenge
from addon.server import build_server
from addon.service import HuntService, UserError
from addon.store import Store
from huntkit import Config
from tests.test_auto import FakeClock, drive, make, start_auto
from tests.test_server_http import free_port

warnings.simplefilter("ignore", ResourceWarning)


def play(seed, events=(), before=(), max_s=700, stop_at=None):
    """One round: hidden leak from `seed`; `before` chaos is applied before the hunt starts; `events` are (seconds, action, arg)
    applied while it runs. Returns (reveal, service, challenge)."""
    svc, clock = make()
    ch = Challenge(svc, "h")
    ch.new_round(seed)
    for a in before:
        ch.act(a)
    clock.advance(1)
    v = start_auto(svc)
    hid, t0, pending = v["hunt_id"], clock(), sorted(events)

    def hook(i, v):
        while pending and pending[0][0] <= clock() - t0:
            _, a, arg = pending.pop(0)
            ch.act(a, arg)
        if stop_at is not None and clock() - t0 >= stop_at:
            svc.stop_hunt("h")

    drive(svc, clock, hid, hook=hook, max_s=max_s)
    return ch.reveal(), svc, ch


class Rounds(unittest.TestCase):
    def test_a_round_hides_the_truth_and_a_seed_makes_it_repeatable(self):
        a, _ = make()[0], None
        ch = Challenge(a, "h")
        st = ch.new_round(3)
        self.assertNotIn("truth", json.dumps(st))
        self.assertTrue(st["hidden"])
        t1 = dict(ch.truth)
        self.assertEqual(a._source("h").house.sources, t1)             # the simulated house really holds it
        ch2 = Challenge(make()[0], "h")
        ch2.new_round(3)
        self.assertEqual(ch2.truth, t1)
        ch2.new_round(4)
        self.assertNotEqual(ch2.truth, t1)

    def test_a_new_round_clears_everything_the_last_one_left(self):
        svc, clock = make()
        ch = Challenge(svc, "h")
        ch.new_round(1)
        ch.act("ambient_on"); ch.act("blackout_on"); ch.act("hub_down"); ch.act("jam", "toilet")
        ch.new_round(1)
        src, hub = svc._source("h"), svc._hub("h")
        self.assertEqual((src.house.extra_lpm, src.blackout, hub.offline, dict(hub.stuck)), (0.0, False, False, {}))
        self.assertEqual(ch.state()["flags"], dict(ambient=False, blackout=False, hub_down=False, jammed=[]))
        self.assertEqual(ch.state()["chaos"], [])

    def test_reveal_needs_a_hunt_from_this_round(self):
        svc, clock = make()
        ch = Challenge(svc, "h")
        with self.assertRaises(UserError):
            ch.reveal()
        ch.new_round(1)
        with self.assertRaises(UserError):
            ch.reveal()
        v = start_auto(svc)
        drive(svc, clock, v["hunt_id"])
        ch.reveal()
        ch.new_round(2)                                                # the old hunt belongs to the old round
        with self.assertRaises(UserError):
            ch.reveal()

    def test_unknown_actions_and_unjammable_pipes_are_refused(self):
        svc, _ = make()
        ch = Challenge(svc, "h")
        with self.assertRaises(UserError):
            ch.act("ambient_on")                                       # before any round
        ch.new_round(1)
        with self.assertRaises(UserError):
            ch.act("explode")
        with self.assertRaises(UserError):
            ch.act("jam", "not-a-pipe")
        self.assertEqual(ch.state()["chaos"], [])


class NothingThrown(unittest.TestCase):
    def test_a_hidden_leak_is_found_and_the_reveal_agrees(self):
        r, svc, _ = play(1)
        self.assertFalse(r["running"])
        self.assertEqual([t["label"] for t in r["truth"]], ["toilet"])
        self.assertEqual(r["engine"]["grade"], "right")
        self.assertEqual(r["engine"]["found"], ["toilet"])
        self.assertTrue(r["verdict"].startswith("Right."))
        self.assertIn("behind toilet", r["verdict"])
        self.assertEqual(r["outside_changes"], 0)
        self.assertTrue(r["within_claimed_bound"])
        self.assertEqual(len(r["evidence_sha256"]), 64)
        self.assertTrue(r["naive"]["same_as_engine"])

    def test_a_leak_below_the_sensors_limit_is_not_pretended_to_be_found(self):
        r, _, _ = play(2)
        self.assertEqual([t["above_limit"] for t in r["truth"]], [False])
        self.assertEqual(r["engine"]["found"], [])
        self.assertEqual(r["engine"]["grade"], "nothing_to_find")
        self.assertIn("below it", r["verdict"])

    def test_a_house_with_no_leak_names_nothing(self):
        r, _, _ = play(6)
        self.assertEqual(r["truth"], [])
        self.assertEqual(r["engine"]["found"], [])
        self.assertIn("nothing above the sensor's limit was hidden", r["verdict"])

    def test_two_hidden_leaks_are_both_isolated(self):
        r, _, _ = play(5)
        self.assertEqual(sorted(t["label"] for t in r["truth"]), ["kitchen pipe 1", "toilet"])
        self.assertEqual(r["engine"]["grade"], "right")
        self.assertEqual(sorted(r["engine"]["found"]), ["kitchen pipe 1", "toilet"])


class Chaos(unittest.TestCase):
    def test_a_sensor_link_that_drops_ends_the_hunt_without_an_accusation_and_the_pipes_come_back(self):
        r, svc, ch = play(1, events=[(60, "blackout_on", None)])
        self.assertEqual(r["hunt_outcome"], "aborted:telemetry_stale")
        self.assertNotEqual(r["engine"]["grade"], "false_accusation")
        self.assertEqual(sorted(svc._source("h").physical_closed()), [])
        self.assertEqual([c["action"] for c in r["you_threw"]], ["blackout_on"])

    def test_a_pipe_controller_that_drops_leaves_nothing_closed_once_it_is_back(self):
        r, svc, ch = play(1, events=[(70, "hub_down", None), (130, "hub_up", None)])
        self.assertNotEqual(r["engine"]["grade"], "false_accusation")
        svc.clock.advance(200)
        svc.tick()
        self.assertEqual(sorted(svc._source("h").physical_closed()), [])    # the hub's own timers reopened whatever was left

    def test_a_jammed_valve_ends_the_hunt_and_never_accuses(self):
        r, svc, _ = play(1, events=[(25, "jam", "toilet")])
        self.assertNotEqual(r["engine"]["grade"], "false_accusation")
        self.assertEqual([c["action"] for c in r["you_threw"]], ["jam"])

    def test_a_sensor_reboot_in_the_middle_does_not_produce_a_false_accusation(self):
        for t in (45, 75, 105):
            r, _, _ = play(4, events=[(t, "reboot_sensor", None)])
            self.assertNotEqual(r["engine"]["grade"], "false_accusation", t)

    def test_one_outside_flow_change_never_produces_a_false_accusation(self):
        # the bound the project claims: a tap that is already running when the hunt starts and is switched off at ANY moment
        for seed in (1, 4, 8):
            for t in (20, 55, 90, 125, 160):
                r, _, _ = play(seed, before=["ambient_on"], events=[(t, "ambient_off", None)], max_s=500)
                self.assertEqual(r["outside_changes"] <= 1, True)
                self.assertTrue(r["within_claimed_bound"])
                self.assertNotEqual(r["engine"]["grade"], "false_accusation", (seed, t, r["engine"]))

    def test_a_tap_that_stops_in_the_middle_of_a_step_is_measured_again_and_the_hunt_finishes(self):
        # Before the fix this hunt said "I'll re-measure" for 30 minutes with the shower closed: the unclean window of the newest
        # step still contained the moment the flow changed, and more readings cannot take it out.
        r, svc, ch = play(7, before=["ambient_on"], events=[(116, "ambient_off", None)], max_s=700)
        self.assertFalse(r["running"])
        self.assertEqual(r["hunt_outcome"], "only_sources")
        self.assertEqual((r["engine"]["grade"], r["engine"]["found"]), ("right", ["shower"]))
        self.assertLessEqual(svc._source("h").physical_closed(), {"shower"})   # nothing but the culprit, held for the question

    def test_without_the_re_measure_that_same_hunt_stalls_with_a_pipe_closed(self):
        # the broken version, so the test above is known to be able to fail
        from unittest import mock
        import addon.service as service_mod
        with mock.patch.object(service_mod, "REMEASURE_WHY", ()):
            r, svc, ch = play(7, before=["ambient_on"], events=[(116, "ambient_off", None)], max_s=700)
        self.assertTrue(r["running"])
        self.assertIn("shower", svc._source("h").physical_closed())            # still closed, 10 minutes on

    def test_chaos_before_the_hunt_does_not_count_as_a_change_during_it(self):
        r, _, _ = play(1, before=["ambient_on"], events=[(40, "ambient_off", None)])
        self.assertEqual(r["outside_changes"], 1)
        self.assertEqual([c["action"] for c in r["you_threw"]], ["ambient_off"])

    def test_a_reveal_in_the_middle_of_a_hunt_is_marked_as_so_far(self):
        svc, clock = make()
        ch = Challenge(svc, "h")
        ch.new_round(1)
        v = start_auto(svc)
        drive(svc, clock, v["hunt_id"], max_s=30)
        r = ch.reveal()
        self.assertTrue(r["running"])
        self.assertIsNone(r["evidence_sha256"])
        self.assertEqual(svc.store.hunt(v["hunt_id"])["ended"], None)    # looking does not end it


class Scripted(unittest.TestCase):
    def test_the_scripted_tap_stop_fools_a_first_drop_engine_and_not_leak_hunt(self):
        svc, clock = make()
        ch = Challenge(svc, "h")
        st = ch.new_round(script="tap_stops")
        self.assertEqual((st["seed"], st["flags"]["ambient"]), (11, True))
        self.assertIn("tap that was already running", st["scripted"])
        v = start_auto(svc)
        drive(svc, clock, v["hunt_id"], max_s=500)
        r = ch.reveal()
        self.assertEqual([t["label"] for t in r["truth"]], ["garden tap"])
        self.assertEqual((r["engine"]["grade"], r["engine"]["found"]), ("right", ["garden tap"]))
        self.assertEqual(r["naive"]["grade"], "false_accusation")                 # 'first drop wins' also accuses a pipe with no leak
        self.assertEqual(r["naive"]["wrong"], ["kitchen pipe 2"])
        self.assertFalse(r["naive"]["same_as_engine"])
        self.assertEqual(r["outside_changes"], 1)                                  # one change: inside the bound
        self.assertTrue(r["within_claimed_bound"])
        self.assertEqual([(c["action"], c["by"]) for c in r["you_threw"]], [("ambient_off", "script")])

    def test_a_slow_customer_meets_the_same_moment_because_the_script_follows_the_hunt(self):
        svc, clock = make()
        ch = Challenge(svc, "h")
        ch.new_round(script="tap_stops")
        v = start_auto(svc)
        for _ in range(40):                                                        # 40 s before anyone says "it's quiet"
            clock.advance(1); svc.tick()
        self.assertTrue(ch.state()["flags"]["ambient"])                            # the tap is still running: nothing fired yet
        drive(svc, clock, v["hunt_id"], max_s=500)
        r = ch.reveal()
        self.assertEqual(r["engine"]["grade"], "right")
        self.assertEqual(r["outside_changes"], 1)

    def test_an_unknown_script_is_refused(self):
        ch = Challenge(make()[0], "h")
        with self.assertRaises(UserError):
            ch.new_round(script="nope")


class OverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for devhost in (False, True):
            clock = FakeClock()
            svc = HuntService(Store(), clock, Config(), key=b"k", public_base_url="http://127.0.0.1")
            svc.ensure_demo_household("default", control=True)
            app = build_server(svc, devhost=devhost).streamable_http_app(json_response=True)
            port = free_port()
            srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
            th = threading.Thread(target=srv.run, daemon=True)
            th.start()
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    urllib.request.urlopen(base + "/healthz", timeout=0.5); break
                except Exception:
                    time.sleep(0.1)
            cls.servers.append((srv, th, base, svc))

    @classmethod
    def tearDownClass(cls):
        for srv, th, _, _ in cls.servers:
            srv.should_exit = True
            th.join(timeout=5)

    @staticmethod
    def call(base, path, body=None, method=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method=method or ("POST" if body is not None else "GET"))
        def parse(raw):
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {"text": raw.decode(errors="replace")}
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, parse(r.read())
        except urllib.error.HTTPError as e:
            return e.code, parse(e.read())

    def test_the_routes_do_not_exist_unless_the_dev_host_is_on(self):
        base = self.servers[0][2]
        self.assertEqual(self.call(base, "/challenge/new", {})[0], 404)
        self.assertEqual(self.call(base, "/challenge/state")[0], 404)
        self.assertEqual(self.call(base, "/devhost")[0], 404)

    def test_a_round_over_http(self):
        base = self.servers[1][2]
        self.assertEqual(self.call(base, "/challenge/reveal")[0], 400)             # nothing yet
        st, body = self.call(base, "/challenge/new", {"seed": 11})
        self.assertEqual(st, 200)
        self.assertEqual(body["round"], 1)
        self.assertNotIn("truth", body)
        st, body = self.call(base, "/challenge/chaos", {"action": "ambient_on"})
        self.assertEqual((st, body["flags"]["ambient"]), (200, True))
        self.assertEqual(self.call(base, "/challenge/chaos", {"action": "nope"})[0], 400)
        self.assertEqual(self.call(base, "/challenge/reveal")[0], 400)             # no hunt in this round yet
        self.assertEqual(self.call(base, "/challenge/new", {"seed": "x"})[0], 400)

    def test_a_request_from_another_machine_is_refused(self):
        import addon.server as srv_mod
        # the guard reads the peer address; prove the rule with a stand-in request object
        class R:
            client = type("C", (), {"host": "10.1.2.3"})()
            method = "GET"
        import asyncio
        mcp = build_server(self.servers[1][3], devhost=True)
        route = [r for r in mcp.streamable_http_app(json_response=True).routes if getattr(r, "path", "") == "/challenge/state"][0]
        resp = asyncio.run(route.endpoint(R()))
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
