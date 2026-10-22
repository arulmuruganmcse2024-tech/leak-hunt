"""Automatic hunts and voice pipe control, on the simulated house with a fake clock.

Everything here is simulation: the hub is addon.actuators.VirtualPipeHub. What it proves is the service's policy
(consent, leases, abort and restore, audit, latch), not the behaviour of any real valve.
"""
import json, os, sys, unittest, warnings
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from huntkit import Config
from addon.service import HuntService, UserError, LEASE_S, MAX_COMMANDS, OFFER_S, PAUSE_TEXT, STALE_ABORT_S
from addon.store import Store

warnings.simplefilter("ignore", ResourceWarning)
T0 = 1_800_000_000.0 + 23 * 3600


class FakeClock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def make(sources=None, manual=None, **kw):
    clock = FakeClock()
    svc = HuntService(Store(), clock, Config(), key=b"test-key", public_base_url="https://x.test")
    svc.ensure_demo_household("h", sources={"toilet": 2.0} if sources is None else sources, control=True, manual=manual, **kw)
    return svc, clock


def start_auto(svc):
    return svc.start_hunt("h", mode="auto", consent=True)


def human_step(svc, hid, hunt_id, v):
    """What the customer does when Alexa asks for their hands (never for a pipe the hub can move)."""
    a = v["action"]
    if a["kind"] == "quiet":
        svc.clock.advance(3); svc.report(hid, hunt_id, "quiet")
    elif a["kind"] in ("close", "open"):
        svc.clock.advance(3); svc.report(hid, hunt_id, a["kind"], (a["close"] or a["open"])[0])
    elif a["kind"] == "set_state":
        svc.clock.advance(3)
        for n in a["close"]: svc.report(hid, hunt_id, "close", n)
        for n in a["open"]: svc.report(hid, hunt_id, "open", n)


def drive(svc, clock, hunt_id, *, hid="h", max_s=900, hook=None, human=True):
    """Tick once a second like the server's ticker; act as the customer only when the hunt needs their hands."""
    v = svc.hunt_status(hid, hunt_id)
    for i in range(max_s):
        clock.advance(1)
        if hook:
            hook(i, v)
        svc.tick()
        v = svc.hunt_status(hid, hunt_id)
        if v["status"] != "active":
            return v
        if human and v["auto"] and v["auto"]["needs_you"]:
            human_step(svc, hid, hunt_id, v)
    return svc.hunt_status(hid, hunt_id)


def physical(svc):
    return sorted(svc._source("h").physical_closed())


class AutoHunt(unittest.TestCase):
    def test_nothing_moves_until_the_customer_agrees(self):
        svc, clock = make()
        c = svc.start_hunt("h")
        self.assertEqual(c["kind"], "choose_mode")
        self.assertIn("Which would you like?", c["speech"])
        n = svc.start_hunt("h", mode="auto")
        self.assertEqual(n["kind"], "needs_consent")
        self.assertIn("until the flow stops", n["consent_text"])
        self.assertIsNone(svc.store.latest_hunt("h"))
        self.assertEqual(svc.store.actuations("h"), [])
        self.assertEqual(physical(svc), [])
        g = svc.start_hunt("h", mode="guided")
        self.assertEqual(g["mode"], "guided")

    def test_full_auto_hunt_finds_the_toilet_asks_before_keeping_it_closed_and_reopens_the_rest(self):
        svc, clock = make()
        v = start_auto(svc)
        self.assertEqual(v["mode"], "auto")
        self.assertIn("Tell me when it's quiet", v["speech"])
        hid = v["hunt_id"]
        v = drive(svc, clock, hid)
        self.assertEqual(v["status"], "done")
        self.assertEqual(v["outcome"], "only_sources")
        self.assertEqual(v["found"], ["toilet"])
        self.assertEqual(physical(svc), ["toilet"])                      # held on a lease while the customer decides; the rest is back on
        self.assertEqual(svc.store.latches("h"), [])                     # NOT latched: nobody has said yes
        self.assertEqual([o["pipe"] for o in svc.store.offers("h", clock())], ["toilet"])
        self.assertIn("I'm holding the toilet off for up to two minutes", v["speech"])
        self.assertIn("Do you want it kept off", v["speech"])
        self.assertEqual(v["auto"]["kept_closed"], [])
        self.assertEqual([(o["id"], o["label"]) for o in v["auto"]["isolation_offer"]], [("toilet", "toilet")])
        self.assertLessEqual(v["auto"]["isolation_offer"][0]["expires_in_s"], 120.0)
        rows = svc.store.actuations("h", hid)
        self.assertTrue(all(r["ok"] for r in rows))
        self.assertTrue({"hunt", "hunt-end"} >= {r["source"] for r in rows})
        # every close made during the hunt, and the hold at its end, is a lease the hub enforces
        self.assertTrue(all(r["detail"].startswith("lease") for r in rows if r["want"] == "closed"))
        self.assertTrue(v["simulated"])
        # an explicit yes is the only thing that makes it a latch
        clock.advance(3)
        out = svc.set_pipe("h", "toilet", "off")
        self.assertEqual(out["kept"], ["toilet"])
        self.assertIn("I'll keep the toilet off until you tell me it's been repaired", out["speech"])
        self.assertEqual([l["pipe"] for l in svc.store.latches("h")], ["toilet"])
        self.assertEqual(svc.store.offers("h", clock()), [])
        clock.advance(LEASE_S * 3)
        svc.tick()
        self.assertEqual(physical(svc), ["toilet"])                      # a kept pipe has no lease: it stays until the customer says on
        now = svc.hunt_status("h", hid)
        self.assertEqual([k["id"] for k in now["auto"]["kept_closed"]], ["toilet"])
        self.assertIn("I've kept the toilet closed, as you asked", now["speech"])

    def test_no_answer_means_the_culprit_comes_back_on_by_itself(self):
        svc, clock = make()
        v = drive(svc, clock, start_auto(svc)["hunt_id"])
        self.assertEqual(physical(svc), ["toilet"])
        clock.advance(OFFER_S + 5)
        svc.tick()                                                         # the hub's lease has lapsed; nobody was asked twice
        self.assertEqual(physical(svc), [])
        self.assertEqual(svc.store.latches("h"), [])
        self.assertEqual(svc.store.offers("h", clock()), [])
        self.assertIn("Everything is back on", svc.hunt_status("h", v["hunt_id"])["speech"])
        # a late "yes" is an ordinary "off", not a latch
        out = svc.set_pipe("h", "toilet", "off")
        self.assertEqual(out["kept"], [])
        self.assertEqual(svc.store.latches("h"), [])

    def test_saying_no_turns_the_culprit_back_on_and_forgets_the_question(self):
        svc, clock = make()
        drive(svc, clock, start_auto(svc)["hunt_id"])
        clock.advance(3)
        out = svc.set_pipe("h", "toilet", "on")
        self.assertEqual(out["kind"], "pipes")                             # no "has it been repaired?" challenge: nothing was latched
        self.assertEqual(physical(svc), [])
        self.assertEqual(svc.store.offers("h", clock()), [])
        self.assertEqual(svc.store.latches("h"), [])

    def test_a_new_hunt_supersedes_an_open_question(self):
        svc, clock = make()
        drive(svc, clock, start_auto(svc)["hunt_id"])
        self.assertTrue(svc.store.offers("h", clock()))
        clock.advance(10)
        start_auto(svc)
        self.assertEqual(svc.store.offers("h", clock()), [])
        self.assertEqual(physical(svc), [])

    def test_each_pipe_is_found_without_the_customer_touching_a_valve(self):
        for leak in ("toilet", "shower", "kitchen1", "kitchen2", "garden"):
            with self.subTest(leak=leak):
                svc, clock = make(sources={leak: 1.8})
                v = drive(svc, clock, start_auto(svc)["hunt_id"])
                self.assertEqual((v["status"], v["found"]), ("done", [leak]), v["speech"])
                self.assertEqual(physical(svc), [leak])
                hand = [r for r in svc.store.reports(v["hunt_id"]) if r["src"] == "user"]
                self.assertEqual([r["kind"] for r in hand], ["quiet"])     # the only human act is 'the house is quiet'

    def test_a_clean_house_moves_no_valve(self):
        svc, clock = make(sources={}, leak_nights=0)
        v = drive(svc, clock, start_auto(svc)["hunt_id"])
        self.assertEqual(v["status"], "done")
        self.assertIn(v["outcome"], ("no_flow_above_limit",))
        self.assertEqual(physical(svc), [])
        self.assertEqual([r for r in svc.store.actuations("h") if r["want"] == "closed"], [])
        self.assertIn("Everything is back on", v["speech"])

    def test_stop_puts_everything_back_at_any_moment(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        v = drive(svc, clock, hid, hook=lambda i, v: None, max_s=70)         # a few pipes are closed by now
        self.assertEqual(v["status"], "active")
        self.assertGreaterEqual(len(physical(svc)), 2)
        out = svc.stop_hunt("h")
        self.assertEqual(out["status"], "stopped")
        self.assertEqual(physical(svc), [])
        self.assertEqual(svc.store.latches("h"), [])                        # a stopped hunt blames nothing
        self.assertIn("put back every pipe", out["speech"])
        again = svc.stop_hunt("h")                                          # idempotent
        self.assertEqual(again["status"], "stopped")
        svc.tick()
        self.assertEqual(physical(svc), [])

    def test_stop_with_no_hunt_is_harmless(self):
        svc, clock = make()
        self.assertIn("no leak hunt", svc.stop_hunt("h")["speech"])

    def test_the_hub_reopens_pipes_by_itself_if_the_server_dies(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        drive(svc, clock, hid, max_s=70)
        self.assertTrue(physical(svc))
        hub = svc._hub("h")
        self.assertTrue(hub.lease_until)
        clock.advance(LEASE_S + 5)                                          # nobody ticks, nobody calls: the add-on is gone
        hub.pipes(clock())                                                  # the device's own watchdog
        self.assertEqual(physical(svc), [])
        svc.tick()                                                          # the add-on returns and finds it was moved
        v = svc.hunt_status("h", hid)
        self.assertEqual((v["status"], v["outcome"]), ("aborted", "aborted:pipe_moved:" + v["outcome"].split(":")[-1]))

    def test_lost_telemetry_aborts_and_reopens(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        drive(svc, clock, hid, max_s=50)
        self.assertTrue(physical(svc))
        src = svc._source("h")
        src.poll = lambda now: []                                           # the sensor goes silent
        v = drive(svc, clock, hid, max_s=200)
        self.assertEqual(v["status"], "aborted")
        self.assertTrue(v["outcome"].startswith("aborted:telemetry_stale"), v["outcome"])
        self.assertEqual(physical(svc), [])
        self.assertIn("lost sight of your water sensor", v["speech"])

    def test_stale_data_pauses_every_valve_command_until_it_aborts_at_the_stop_limit(self):
        """Between cfg.stale_s (35 s) and STALE_ABORT_S (60 s) the hunt is paused: no inference, and no valve command of
        any kind, renewals included. At 60 s it stops and puts everything back."""
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        drive(svc, clock, hid, max_s=50)
        self.assertTrue(physical(svc))
        svc._source("h").poll = lambda now: []                              # the sensor goes silent
        last = svc._last_reading("h").t
        paused_at = aborted_at = count = None
        for _ in range(120):
            clock.advance(1)
            svc.tick()
            v = svc.hunt_status("h", hid)
            age = clock() - last
            if v["status"] == "active" and v["auto"]["paused"] and paused_at is None:
                paused_at, count = age, len(svc.store.actuations("h"))
                self.assertEqual(v["speech"], PAUSE_TEXT)
                self.assertLessEqual(v["auto"]["paused"]["stops_in_s"], STALE_ABORT_S - svc.cfg.stale_s + 1)
            if v["status"] == "active" and paused_at is not None:
                self.assertEqual(len(svc.store.actuations("h")), count, f"a valve command was sent at data age {age:.0f} s")
            if v["status"] != "active":
                aborted_at = age
                break
        self.assertIsNotNone(paused_at)
        self.assertGreater(paused_at, svc.cfg.stale_s)
        self.assertLess(paused_at, svc.cfg.stale_s + 4)
        self.assertGreaterEqual(aborted_at, STALE_ABORT_S)
        self.assertLess(aborted_at, STALE_ABORT_S + 4)
        self.assertEqual(svc.hunt_status("h", hid)["outcome"], "aborted:telemetry_stale")
        self.assertEqual(physical(svc), [])

    def test_a_pause_that_ends_resumes_the_hunt(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        drive(svc, clock, hid, max_s=50)
        src = svc._source("h")
        real_poll = src.poll
        src.poll = lambda now: []
        paused = False
        for _ in range(45):                                                  # 45 s without data: paused, not stopped
            clock.advance(1); svc.tick()
            v = svc.hunt_status("h", hid)
            paused = paused or bool(v["auto"] and v["auto"]["paused"])
        self.assertTrue(paused)
        self.assertEqual(v["status"], "active")
        src.poll = real_poll                                                  # the sensor is back
        v = drive(svc, clock, hid, max_s=900)
        self.assertEqual(v["status"], "done")
        self.assertIsNone(v["auto"]["paused"])

    def test_a_stuck_valve_aborts_and_the_rest_come_back(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        hub = svc._hub("h")
        def hook(i, v):
            if physical(svc) == ["toilet"] and "shower" not in hub.stuck:
                hub.stuck["shower"] = "open"                                 # the next valve will not close
        v = drive(svc, clock, hid, hook=hook)
        self.assertEqual(v["status"], "aborted")
        self.assertIn("pipe_fault:shower", v["outcome"])
        self.assertIn("shower didn't move", v["speech"])
        self.assertEqual(physical(svc), [])
        self.assertEqual(svc.store.latches("h"), [])

    def test_an_unreachable_hub_ends_the_hunt_and_the_leases_cover_the_gap(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        drive(svc, clock, hid, max_s=50)
        hub = svc._hub("h")
        hub.offline = True
        v = drive(svc, clock, hid, max_s=60)
        self.assertEqual(v["status"], "aborted")
        self.assertIn("hub_unreachable", v["outcome"])
        self.assertTrue(physical(svc))                                      # we could not reach it to put them back
        clock.advance(LEASE_S + 5)
        svc.tick()
        self.assertEqual(physical(svc), [])                                 # the hub's own watchdog did

    def test_the_devices_own_safety_valve_closing_ends_the_hunt_and_reopens_pipes(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        drive(svc, clock, hid, max_s=50)
        src = svc._source("h")
        src.house.cutoff()                                                  # the device trips its own valve
        v = drive(svc, clock, hid, max_s=60)
        self.assertEqual(v["status"], "aborted")
        self.assertEqual(v["outcome"], "device_safety_valve_closed")
        self.assertEqual(physical(svc), [])
        self.assertIn("put every pipe I closed back", v["speech"])

    def test_hand_valves_stay_with_the_customer(self):
        svc, clock = make({"toilet": 2.0}, manual=["toilet"])
        hid = start_auto(svc)["hunt_id"]
        seen = {"needs": 0}
        def hook(i, v):
            if v["auto"] and v["auto"]["needs_you"] and v["action"]["kind"] != "quiet":
                seen["needs"] += 1
                self.assertIn("I need your hands", v["speech"])
        v = drive(svc, clock, hid, hook=hook)
        self.assertEqual((v["status"], v["found"]), ("done", ["toilet"]))
        self.assertGreater(seen["needs"], 0)
        self.assertEqual(svc.store.latches("h"), [])                        # a hand valve cannot be latched by software
        # the pipes the hub owns were never closed on the customer's behalf except while measuring
        self.assertEqual([p for p in physical(svc) if p != "toilet"], [])

    def test_the_customer_cannot_report_a_pipe_the_hub_moves_and_cannot_undo_a_hub_step(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        drive(svc, clock, hid, max_s=30)
        out = svc.report("h", hid, "close", "toilet")
        self.assertIn("switching that pipe myself", out["ignored"])
        out = svc.report("h", hid, "undo")
        self.assertIn("nothing to undo", out["ignored"])
        out = svc.report("h", hid, "quiet")
        self.assertIn("already running", out["ignored"])
        self.assertEqual([r["src"] for r in svc.store.reports(hid)].count("user"), 1)

    def test_a_house_that_never_goes_quiet_stops_instead_of_guessing(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        house = svc._source("h").house
        def hook(i, v):
            house.extra_lpm = 2.0 if (i // 17) % 2 == 0 else 0.0           # someone keeps using water
        v = drive(svc, clock, hid, hook=hook, max_s=1500)
        self.assertIn(v["status"], ("done", "aborted"))
        if v["status"] == "done":                                            # whatever it says must not be a wrong accusation
            self.assertNotIn("toilet", v["found"])
            self.assertNotIn("shower", v["found"])
        self.assertEqual([p for p in physical(svc) if p not in v["found"]], [])

    def test_the_command_budget_is_enforced(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        hunt = svc.store.hunt(hid)
        for i in range(MAX_COMMANDS):
            svc.store.add_actuation("h", hid, clock(), "toilet", "closed", "hunt", True, "")
        v = drive(svc, clock, hid, max_s=200)
        self.assertEqual(v["status"], "aborted")
        self.assertIn("command_cap", v["outcome"])
        self.assertEqual(physical(svc), [])

    def test_a_pipe_is_never_flipped_twice_in_two_seconds(self):
        svc, clock = make()
        a = svc._command("h", None, "toilet", "closed", "voice")
        b = svc._command("h", None, "toilet", "open", "voice")
        self.assertTrue(a.ok)
        self.assertEqual((b.ok, b.error), (False, "rate_limited"))
        clock.advance(2.5)
        self.assertTrue(svc._command("h", None, "toilet", "open", "voice").ok)

    def test_evidence_lists_every_valve_command_and_still_replays(self):
        svc, clock = make()
        hid = start_auto(svc)["hunt_id"]
        v = drive(svc, clock, hid)
        p = svc.evidence("h", hid)
        self.assertEqual(p["mode"], "auto")
        self.assertEqual(len(p["actuations"]), len(svc.store.actuations("h", hid)))
        self.assertEqual(svc.verify_packet(p), dict(hash_ok=True, hmac_ok=True, replay_matches=True))
        html = svc.evidence_html(p)
        self.assertIn("Valve commands sent", html)
        self.assertIn("AUTOMATIC HUNT", html)
        self.assertIn("Alexa, via the pipe hub", html)
        self.assertEqual({r["by"] for r in p["reports"]}, {"user", "hub"})

    def test_a_latched_culprit_blocks_the_next_automatic_hunt_until_reopened(self):
        svc, clock = make()
        drive(svc, clock, start_auto(svc)["hunt_id"])
        clock.advance(3)
        svc.set_pipe("h", "toilet", "off")                                  # the customer said yes: keep it closed
        clock.advance(10)
        with self.assertRaises(UserError) as e:
            start_auto(svc)
        self.assertIn("still closed because of the leak I found", str(e.exception))
        ask = svc.set_pipe("h", "toilet", "on")
        self.assertEqual((ask["kind"], ask["reason"]), ("needs_confirm", "latched"))
        self.assertEqual(physical(svc), ["toilet"])
        done = svc.set_pipe("h", "toilet", "on", confirm=True)
        self.assertEqual(done["kind"], "pipes")
        self.assertEqual(physical(svc), [])
        self.assertEqual(svc.store.latches("h"), [])

    def test_a_mid_hunt_restart_loses_nothing_and_never_leaves_a_pipe_unleased(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        drive(svc, clock, hid, max_s=50)
        # a "new process": new service object over the same store; the virtual hub is gone, so the add-on finds
        # the hunt open but cannot confirm positions through it - it must end the hunt, not guess.
        svc2 = HuntService(svc.store, clock, Config(), key=b"test-key")
        svc2._sources = svc._sources                                        # the physical (simulated) house is the same house
        v = drive(svc2, clock, hid, max_s=400)
        self.assertEqual(v["status"], "done")
        self.assertEqual(v["found"], ["garden"])


class VoicePipes(unittest.TestCase):
    def test_numbered_names_and_the_question_when_two_pipes_fit(self):
        svc, clock = make()
        p = svc.list_pipes("h")
        self.assertIn("kitchen pipe 1", p["speech"])
        self.assertIn("kitchen pipe 2", p["speech"])
        self.assertIn("All of them are on", p["speech"])
        ask = svc.set_pipe("h", "kitchen", "on")
        self.assertEqual(ask["kind"], "choose_pipe")
        self.assertIn("kitchen pipe 1 or kitchen pipe 2", ask["speech"])
        self.assertEqual([o["id"] for o in ask["options"]], ["kitchen1", "kitchen2"])
        self.assertEqual(physical(svc), [])                                 # a question is not a command

    def test_turn_off_one_numbered_pipe(self):
        svc, clock = make()
        out = svc.set_pipe("h", "kitchen pipe 2", "off")
        self.assertEqual(out["speech"], "Kitchen pipe 2 is off.")
        self.assertEqual(physical(svc), ["kitchen2"])
        rows = {r["id"]: r for r in out["pipes"]}
        self.assertEqual(rows["kitchen2"]["position"], "closed")
        self.assertEqual(rows["kitchen1"]["position"], "open")
        self.assertIn("Right now kitchen pipe 2 is off", svc.list_pipes("h")["speech"])
        clock.advance(3)
        out = svc.set_pipe("h", "the second kitchen pipe", "on")
        self.assertEqual(physical(svc), [])

    def test_all_in_the_room_and_by_alias(self):
        svc, clock = make()
        out = svc.set_pipe("h", "all kitchen pipes", "off")
        self.assertEqual(physical(svc), ["kitchen1", "kitchen2"])
        self.assertEqual(out["speech"], "Kitchen pipe 1 and kitchen pipe 2 are off.")
        clock.advance(3)
        svc.set_pipe("h", "kitchen", "on", all_in_room=True)
        self.assertEqual(physical(svc), [])
        clock.advance(3)
        svc.set_pipe("h", "the dishwasher", "off")
        self.assertEqual(physical(svc), ["kitchen2"])

    def test_the_bathroom_is_its_branch_valve(self):
        svc, clock = make()
        svc.set_pipe("h", "bathroom", "off")
        self.assertEqual(physical(svc), ["bath"])

    def test_unknown_names_list_what_exists(self):
        svc, clock = make()
        with self.assertRaises(UserError) as e:
            svc.set_pipe("h", "kitchen pipe 3", "on")
        self.assertIn("In the kitchen I have kitchen pipe 1 and kitchen pipe 2", str(e.exception))
        with self.assertRaises(UserError):
            svc.set_pipe("h", "toilet", "maybe")

    def test_hand_valves_and_protected_pipes_are_never_switched(self):
        svc, clock = make(manual=["garden"])
        with self.assertRaises(UserError) as e:
            svc.set_pipe("h", "garden tap", "off")
        self.assertIn("hand valve", str(e.exception))
        self.assertIn("by the back door", str(e.exception))
        h = svc.household("h")
        topo = [dict(r, protect=(r["id"] == "toilet")) for r in h["topology"]]
        svc.set_household("h", h["name"], topo)
        with self.assertRaises(UserError) as e:
            svc.set_pipe("h", "toilet", "off")
        self.assertIn("never switch", str(e.exception))
        self.assertEqual(physical(svc), [])

    def test_moving_many_pipes_needs_a_yes(self):
        svc, clock = make()
        ask = svc.set_pipe("h", "everything", "off")
        self.assertEqual((ask["kind"], ask["reason"]), ("needs_confirm", "many_pipes"))
        self.assertEqual(physical(svc), [])
        svc.set_pipe("h", "everything", "off", confirm=True)
        self.assertEqual(len(physical(svc)), 6)

    def test_a_pipe_command_during_a_hunt_asks_before_ending_it(self):
        svc, clock = make({"garden": 2.0})
        hid = start_auto(svc)["hunt_id"]
        drive(svc, clock, hid, max_s=30)
        clock.advance(3)
        ask = svc.set_pipe("h", "kitchen pipe 1", "off")
        self.assertEqual((ask["kind"], ask["reason"]), ("needs_confirm", "hunt_active"))
        done = svc.set_pipe("h", "kitchen pipe 1", "off", confirm=True)
        self.assertEqual(physical(svc), ["kitchen1"])                         # the hunt's pipes came back; the customer's switch stayed
        self.assertEqual(svc.hunt_status("h", hid)["status"], "stopped")

    def test_opening_is_refused_while_the_main_safety_valve_is_closed(self):
        svc, clock = make()
        svc.set_pipe("h", "toilet", "off")
        svc._source("h").house.cutoff()
        clock.advance(15)
        svc.water_status("h")                                                # a fresh reading shows CLOSED
        clock.advance(3)
        with self.assertRaises(UserError) as e:
            svc.set_pipe("h", "toilet", "on")
        self.assertIn("main safety valve", str(e.exception))
        clock.advance(3)
        svc.set_pipe("h", "kitchen pipe 1", "off")                           # closing is always allowed

    def test_flipping_a_pipe_back_immediately_is_declined_politely(self):
        svc, clock = make()
        svc.set_pipe("h", "toilet", "off")
        with self.assertRaises(UserError) as e:
            svc.set_pipe("h", "toilet", "on")
        self.assertIn("couple of seconds", str(e.exception))

    def test_every_voice_command_is_in_the_audit_log(self):
        svc, clock = make()
        svc.set_pipe("h", "toilet", "off")
        rows = svc.store.actuations("h")
        self.assertEqual([(r["pipe"], r["want"], r["source"], r["ok"]) for r in rows], [("toilet", "closed", "voice", True)])

    def test_no_hub_no_control(self):
        svc, clock = make()
        svc.configure_pipehub("h", None)
        self.assertFalse(svc.control_available("h"))
        self.assertIn("can't switch pipes", svc.list_pipes("h")["speech"])
        with self.assertRaises(UserError):
            svc.set_pipe("h", "toilet", "off")
        self.assertEqual(svc.start_hunt("h")["kind"], "hunt")                # guided straight away


if __name__ == "__main__":
    unittest.main()


class SpeechMatchesWhoActs(unittest.TestCase):
    """The screen and the voice must never ask a person for what Alexa is about to do herself."""

    def test_no_hands_requested_when_the_hub_moves_the_pipe_even_with_a_slow_server(self):
        svc, clock = make()
        orig = svc._drive

        def slow(hunt_id, now):                  # a request that takes 1.5 s: the clock moves while the hub is working
            orig(hunt_id, now)
            clock.advance(1.5)
        svc._drive = slow
        hid = start_auto(svc)["hunt_id"]
        seen = 0
        v = svc.hunt_status("h", hid)
        for _ in range(400):
            clock.advance(1)
            svc.tick()
            v = svc.hunt_status("h", hid)
            if "I need your hands" in v["speech"]:
                self.assertTrue(v["auto"]["needs_you"], v["speech"])
            if v["status"] == "active" and v["action"]["kind"] in ("close", "open", "set_state") and not v["auto"]["needs_you"]:
                # the engine already wants the next pipe moved but the next tick has not run yet: say so, never hand it to the person
                self.assertIn("I'm moving the next pipe now", v["speech"])
                self.assertNotIn(v["action"]["text"], v["speech"])
            seen += 1
            if v["status"] != "active":
                break
            if v["auto"]["needs_you"]:
                human_step(svc, "h", hid, v)
        self.assertEqual((v["status"], v["found"]), ("done", ["toilet"]))
