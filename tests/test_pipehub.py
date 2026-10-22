"""RestPipeHub (the add-on's client) against tools/pipehub_emulator.py, over real HTTP on localhost.

The emulator is a software model of the contract in docs/PIPE_HUB.md. Nothing here touches a real board, relay, valve or
water; it proves that the client and the service obey the contract and the safety policy when the hub behaves, and when it
does not (slow, stuck, unreachable, rebooted, wrong token).
"""
import os, sys, unittest, warnings
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from huntkit import Config
from addon.actuators import RestPipeHub, CLOSED, OPEN, MAX_LEASE_S
from addon.service import HuntService, LEASE_S
from addon.store import Store
from pipehub_emulator import PipeHubEmulator
from tests.test_auto import FakeClock, drive, human_step

warnings.simplefilter("ignore", ResourceWarning)
PIPES = ["kitchen1", "kitchen2", "toilet"]


class Contract(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.emu = PipeHubEmulator(PIPES, "tok", clock=self.clock).start()
        self.hub = RestPipeHub(self.emu.base_url, "tok", self.clock, timeout=2.0,
                               sleep=lambda s: self.clock.advance(s))
        self.addCleanup(self.emu.stop)

    def test_list_and_membership(self):
        st = self.hub.pipes(self.clock())
        self.assertEqual(sorted(st), sorted(PIPES))
        self.assertTrue(all(s.position == OPEN and s.lease_left_s is None for s in st.values()))
        self.assertTrue(self.hub.has("toilet"))
        self.assertFalse(self.hub.has("main"))
        self.assertEqual(sorted(self.hub.pipe_ids()), sorted(PIPES))

    def test_close_with_a_lease_then_the_hub_reopens_it_by_itself(self):
        a = self.hub.command("kitchen1", CLOSED, self.clock(), 120)
        self.assertTrue(a.ok); self.assertEqual(a.position, CLOSED)
        self.assertEqual(self.emu.closed_pipes(), ["kitchen1"])
        self.assertAlmostEqual(self.hub.pipes(self.clock())["kitchen1"].lease_left_s, 120, delta=0.5)
        self.clock.advance(119)
        self.assertEqual(self.emu.closed_pipes(), ["kitchen1"])
        self.clock.advance(2)                              # nobody called the hub: its own clock reopened the pipe
        self.assertEqual(self.emu.closed_pipes(), [])
        self.assertEqual(self.hub.pipes(self.clock())["kitchen1"].position, OPEN)
        self.assertAlmostEqual(self.emu.max_closed_run_s("kitchen1"), 120, delta=0.5)

    def test_open_clears_the_lease(self):
        self.hub.command("kitchen1", CLOSED, self.clock(), 120)
        self.clock.advance(10)
        a = self.hub.command("kitchen1", OPEN, self.clock(), None)
        self.assertTrue(a.ok)
        self.assertIsNone(self.hub.pipes(self.clock())["kitchen1"].lease_left_s)
        self.clock.advance(500)
        self.assertEqual(self.emu.closed_pipes(), [])

    def test_a_lease_longer_than_the_cap_is_capped_by_client_and_hub(self):
        self.hub.command("kitchen1", CLOSED, self.clock(), 10_000)
        self.assertLessEqual(self.emu.log[-1]["lease_s"], MAX_LEASE_S)
        raw = self.emu.handle("PUT", "/api/v1/pipes/kitchen2", {"want": "closed", "lease_s": 99_999})     # a client that skips the cap
        self.assertEqual(raw[0], 200)
        self.assertLessEqual(raw[1]["lease_s_left"], MAX_LEASE_S)

    def test_a_close_without_a_lease_is_held_until_changed(self):
        self.hub.command("kitchen2", CLOSED, self.clock(), None)
        self.clock.advance(10_000)
        self.assertEqual(self.emu.closed_pipes(), ["kitchen2"])

    def test_bad_requests_are_refused_and_never_raise(self):
        a = self.hub.command("main", CLOSED, self.clock(), 60)
        self.assertEqual((a.ok, a.error), (False, "http_404"))
        a = self.hub.command("kitchen1", "sideways", self.clock(), 60)
        self.assertEqual((a.ok, a.error), (False, "bad_command"))
        self.assertEqual(self.emu.log, [])
        for bad in ({"want": "closed", "lease_s": -5}, {"want": "closed", "lease_s": "soon"}, {"want": "closed", "lease_s": True},
                    {"want": "ajar"}, {}):
            self.assertEqual(self.emu.handle("PUT", "/api/v1/pipes/kitchen1", bad)[0], 400, bad)
        self.assertEqual(self.emu.closed_pipes(), [])

    def test_wrong_token_changes_nothing(self):
        bad = RestPipeHub(self.emu.base_url, "wrong", self.clock, timeout=2.0)
        a = bad.command("kitchen1", CLOSED, self.clock(), 60)
        self.assertEqual((a.ok, a.error), (False, "http_401"))
        self.assertEqual(self.emu.closed_pipes(), [])
        self.assertEqual(self.emu.log, [])                              # refused before anything was recorded as a command

    def test_http_to_a_remote_host_is_refused(self):
        with self.assertRaises(ValueError):
            RestPipeHub("http://hub.example.com", "t", self.clock)
        RestPipeHub("https://hub.example.com", "t", self.clock)
        RestPipeHub("http://192.168.1.50", "t", self.clock, allow_insecure_lan=True)
        RestPipeHub("http://127.0.0.1:9", "t", self.clock)

    def test_an_unreachable_hub_is_an_error_not_an_exception(self):
        self.emu.offline = True
        a = self.hub.command("kitchen1", CLOSED, self.clock(), 60)
        self.assertFalse(a.ok); self.assertTrue(a.error)
        with self.assertRaises(Exception):
            self.hub.pipes(self.clock())

    def test_an_http_error_is_reported(self):
        self.emu.http_error = 503
        a = self.hub.command("kitchen1", CLOSED, self.clock(), 60)
        self.assertEqual((a.ok, a.error), (False, "http_503"))

    def test_a_stuck_valve_reports_a_fault_and_no_ack(self):
        self.emu.inject("kitchen1", stuck=True)
        a = self.hub.command("kitchen1", CLOSED, self.clock(), 60)
        self.assertEqual((a.ok, a.position, a.error), (False, "fault", "fault"))
        self.assertEqual(self.emu.closed_pipes(), [])

    def test_travel_time_is_waited_for_and_measured(self):
        self.emu.travel_s = 2.0
        t0 = self.clock()
        a = self.hub.command("kitchen1", CLOSED, self.clock(), 60)
        self.assertTrue(a.ok)
        self.assertAlmostEqual(a.t_ack - t0, 2.0, delta=0.5)         # the ack time is when the position was MEASURED closed

    def test_a_valve_that_takes_too_long_is_not_acked(self):
        self.emu.travel_s = 20.0
        a = self.hub.command("kitchen1", CLOSED, self.clock(), 60)
        self.assertEqual((a.ok, a.error, a.position), (False, "no_ack", "moving"))

    def test_hub_reboot_returns_every_pipe_to_open(self):
        self.hub.command("kitchen1", CLOSED, self.clock(), None)
        self.hub.command("toilet", CLOSED, self.clock(), 100)
        self.emu.reboot()
        self.assertEqual(self.emu.closed_pipes(), [])
        self.assertTrue(all(s.lease_left_s is None for s in self.hub.pipes(self.clock()).values()))

    def test_leases_are_independent_and_expire_in_order(self):
        self.hub.command("kitchen1", CLOSED, self.clock(), 30)
        self.clock.advance(10)
        self.hub.command("kitchen2", CLOSED, self.clock(), 30)
        self.clock.advance(25)
        self.assertEqual(self.emu.closed_pipes(), ["kitchen2"])
        self.clock.advance(10)
        self.assertEqual(self.emu.closed_pipes(), [])


def chain(travel_s=0.0, pipes=None, sources=None):
    """HuntService -> RestPipeHub -> HTTP -> emulator -> VirtualSource: the whole path, minus real hardware."""
    clock = FakeClock()
    svc = HuntService(Store(), clock, Config(), key=b"k", public_base_url="https://x.test")
    svc.ensure_demo_household("h", sources={"toilet": 2.0} if sources is None else sources, control=False)
    emu = PipeHubEmulator(pipes or ["bath", "toilet", "shower", "kitchen1", "kitchen2", "garden"], "tok", clock=clock,
                          travel_s=travel_s, on_change=lambda p, c, at: svc._source("h").set_closed(p, c, at)).start()
    svc.configure_pipehub("h", {"kind": "rest", "enabled": True, "base_url": emu.base_url, "token": "tok"})
    return svc, clock, emu


class Chain(unittest.TestCase):
    def test_auto_hunt_through_the_http_hub(self):
        svc, clock, emu = chain()
        self.addCleanup(emu.stop)
        v = svc.start_hunt("h", mode="auto", consent=True)
        v = drive(svc, clock, v["hunt_id"])
        self.assertEqual((v["status"], v["outcome"], v["found"]), ("done", "only_sources", ["toilet"]))
        self.assertEqual(emu.closed_pipes(), ["toilet"])                  # the confirmed culprit is held on a lease for the question
        self.assertLessEqual(emu.max_closed_run_s("toilet"), LEASE_S + 1)
        for p in ("bath", "shower", "kitchen1", "kitchen2", "garden"):
            self.assertLessEqual(emu.max_closed_run_s(p), LEASE_S + 1, p)  # nothing was ever held beyond its lease
        self.assertTrue(all(c["lease_s"] is None or c["lease_s"] <= LEASE_S for c in emu.log))
        self.assertGreaterEqual(len([c for c in emu.log if c["want"] == "closed"]), 2)
        acts = svc.store.actuations("h")
        self.assertTrue(all(a["ok"] for a in acts))

    def test_every_close_carries_a_lease(self):
        svc, clock, emu = chain()
        self.addCleanup(emu.stop)
        v = svc.start_hunt("h", mode="auto", consent=True)
        drive(svc, clock, v["hunt_id"])
        closes = [c for c in emu.log if c["want"] == "closed"]
        self.assertTrue(closes)
        self.assertEqual([c for c in closes if c["lease_s"] is None], [])      # nothing is closed without a lease, the end of the hunt included
        self.assertEqual(closes[-1]["pipe"], "toilet")                          # the culprit is held on a fresh lease while the customer decides
        self.assertEqual(closes[-1]["lease_s"], 120.0)
        self.assertEqual(svc.store.latches("h"), [])
        # the one close without a lease exists only after an explicit yes, and is audited as the customer's own command
        clock.advance(3)
        svc.set_pipe("h", "toilet", "off")
        unleased = [c for c in emu.log if c["want"] == "closed" and c["lease_s"] is None]
        self.assertEqual([c["pipe"] for c in unleased], ["toilet"])
        voice = [a for a in svc.store.actuations("h") if a["want"] == "closed" and a["detail"] == ""]
        self.assertEqual([(a["pipe"], a["source"]) for a in voice], [("toilet", "voice")])

    def test_stop_names_the_pipe_it_could_not_confirm_back_on(self):
        svc, clock, emu = chain()
        self.addCleanup(emu.stop)
        v = svc.start_hunt("h", mode="auto", consent=True)
        hid = v["hunt_id"]
        clock.advance(3); svc.report("h", hid, "quiet")
        for _ in range(300):
            clock.advance(1); svc.tick()
            if emu.closed_pipes():
                break
        closed = emu.closed_pipes()
        self.assertTrue(closed)
        emu.inject(closed[0], stuck=True)                     # the valve jams shut, so Stop cannot put it back
        out = svc.stop_hunt("h")
        self.assertEqual(out["status"], "stopped")
        self.assertEqual([r["id"] for r in out["auto"]["not_restored"]], closed)
        self.assertIn("I couldn't confirm", out["speech"])
        self.assertIn("reopens it by itself within", out["speech"])
        self.assertNotIn("I've put back every pipe I closed", out["speech"])        # it did not; it says what it did instead
        self.assertIn("I've told your pipe controller to put back every pipe I closed", out["speech"])
        self.assertLessEqual(out["auto"]["not_restored"][0]["reopens_within_s"], LEASE_S)

    def test_a_clean_stop_reports_nothing_unrestored(self):
        svc, clock, emu = chain()
        self.addCleanup(emu.stop)
        v = svc.start_hunt("h", mode="auto", consent=True)
        hid = v["hunt_id"]
        clock.advance(3); svc.report("h", hid, "quiet")
        for _ in range(300):
            clock.advance(1); svc.tick()
            if emu.closed_pipes():
                break
        out = svc.stop_hunt("h")
        self.assertEqual(out["auto"]["not_restored"], [])
        self.assertEqual(emu.closed_pipes(), [])

    def test_the_hub_goes_dark_mid_hunt_and_the_pipes_come_back_by_themselves(self):
        svc, clock, emu = chain()
        self.addCleanup(emu.stop)
        v = svc.start_hunt("h", mode="auto", consent=True)
        hid = v["hunt_id"]
        clock.advance(3); svc.report("h", hid, "quiet")
        for _ in range(300):                                 # run until something is closed
            clock.advance(1); svc.tick()
            if emu.closed_pipes():
                break
        self.assertTrue(emu.closed_pipes())
        emu.offline = True                                    # the network (or the hub's Wi-Fi) dies; the server can no longer reach it
        for _ in range(60):
            clock.advance(1); svc.tick()
        s = svc.hunt_status("h", hid)
        self.assertNotEqual(s["status"], "active")            # the hunt gave up (unreachable > 15 s)
        emu.offline = False
        clock.advance(LEASE_S + 5)                            # ... and the HUB's own clock reopened whatever the server left closed
        self.assertEqual(emu.closed_pipes(), [])

    def test_a_slow_valve_aborts_the_hunt_and_everything_is_restored(self):
        svc, clock, emu = chain(travel_s=7.0)                 # slower than act_guard_s (5 s)
        self.addCleanup(emu.stop)
        svc._hubs["h"] = RestPipeHub(emu.base_url, "tok", clock, ack_timeout_s=12.0, sleep=lambda s: clock.advance(s))
        v = svc.start_hunt("h", mode="auto", consent=True)
        hid = v["hunt_id"]
        clock.advance(3); svc.report("h", hid, "quiet")
        for _ in range(200):
            clock.advance(1); svc.tick()
            if svc.hunt_status("h", hid)["status"] != "active":
                break
        s = svc.hunt_status("h", hid)
        self.assertEqual(s["status"], "aborted")
        self.assertTrue(s["outcome"].startswith("aborted:pipe_too_slow"))
        clock.advance(20)
        self.assertEqual(emu.closed_pipes(), [])

    def test_a_stuck_valve_stops_the_hunt_and_accuses_nothing(self):
        svc, clock, emu = chain()
        self.addCleanup(emu.stop)
        emu.inject("kitchen1", stuck=True)
        v = svc.start_hunt("h", mode="auto", consent=True)
        v = drive(svc, clock, v["hunt_id"])
        self.assertEqual(v["status"], "aborted")
        self.assertEqual(v["outcome"], "aborted:pipe_fault:kitchen1")
        self.assertEqual(v["found"], [])
        self.assertIn("didn't move", v["speech"])
        self.assertEqual(emu.closed_pipes(), [])
        self.assertFalse(svc.store.latches("h"))


if __name__ == "__main__":
    unittest.main()


class Outlets(unittest.TestCase):
    """An outlet (a bath tap valve the add-on fills from) rests CLOSED: it opens only on a lease, at most 60 s, and the hub's
    own clock closes it again. Software model of the REST contract; no board, no valve, no water."""

    def setUp(self):
        self.clock = FakeClock()
        self.emu = PipeHubEmulator(["bath", "toilet"], "tok", clock=self.clock, outlets=["tub"])

    def test_an_outlet_rests_closed_and_never_opens_without_a_lease(self):
        self.assertEqual(self.emu.handle("GET", "/api/v1/pipes/tub", None)[1]["position"], "closed")
        code, body = self.emu.handle("PUT", "/api/v1/pipes/tub", {"want": "open", "lease_s": None})
        self.assertEqual(code, 400)
        code, body = self.emu.handle("PUT", "/api/v1/pipes/tub", {"want": "open", "lease_s": 999})
        self.assertEqual((code, body["position"]), (200, "open"))
        self.assertLessEqual(body["lease_s_left"], 60.0)

    def test_the_hubs_own_clock_closes_an_outlet_and_a_reboot_closes_it(self):
        self.emu.handle("PUT", "/api/v1/pipes/tub", {"want": "open", "lease_s": 20})
        self.clock.advance(21)
        self.assertEqual(self.emu.handle("GET", "/api/v1/pipes/tub", None)[1]["position"], "closed")
        self.emu.handle("PUT", "/api/v1/pipes/tub", {"want": "open", "lease_s": 20})
        self.emu.reboot()
        self.assertEqual(self.emu.handle("GET", "/api/v1/pipes/tub", None)[1]["position"], "closed")

    def test_branch_pipes_behave_as_before(self):
        self.emu.handle("PUT", "/api/v1/pipes/toilet", {"want": "closed", "lease_s": 30})
        self.clock.advance(31)
        self.assertEqual(self.emu.handle("GET", "/api/v1/pipes/toilet", None)[1]["position"], "open")
        self.assertEqual(self.emu.closed_pipes(), [])


class FillOverHttp(unittest.TestCase):
    def test_a_fill_through_the_http_hub(self):
        clock = FakeClock()
        svc = HuntService(Store(), clock, Config(), key=b"k", public_base_url="https://x.test")
        svc.ensure_demo_household("h", sources={"toilet": 2.0}, control=False, outlets=True)
        src = svc._source("h")
        emu = PipeHubEmulator(["bath", "toilet", "shower", "kitchen1", "kitchen2", "garden"], "tok", clock=clock, outlets=["tub"],
                              on_change=lambda p, c, at: src.set_closed(p, c, at),
                              on_outlet=lambda p, o, at: src.set_outlet(p, o, at)).start()
        self.addCleanup(emu.stop)
        svc.configure_pipehub("h", {"kind": "rest", "enabled": True, "base_url": emu.base_url, "token": "tok"})
        src = svc._source("h")                                     # configure_pipehub rebuilt the source; rewire the emulator
        emu.on_change = lambda p, c, at: src.set_closed(p, c, at)
        emu.on_outlet = lambda p, o, at: src.set_outlet(p, o, at)
        clock.advance(30); svc.tick()
        svc.start_fill("h", "bath", 30)
        for _ in range(600):
            clock.advance(1); svc.tick()
            if svc.store.latest_fill("h")["ended"] is not None:
                break
        f = svc.store.latest_fill("h")
        self.assertEqual(f["outcome"], "done")
        self.assertAlmostEqual(src.delivered_l("tub", clock()), 30.0, delta=1.5)
        self.assertTrue(all(e["lease_s"] is not None and e["lease_s"] <= 60 for e in emu.log if e["pipe"] == "tub" and e["want"] == "open"))
