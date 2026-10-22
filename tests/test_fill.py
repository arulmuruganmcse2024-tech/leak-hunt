"""Filling by volume ("Alexa, fill the bath, 120 litres") on the simulated house, with a fake clock.

Simulation only: the bath tap valve, its flow and the meter are models (addon/sources.py, addon/actuators.py). The ground truth
(`VirtualSource.delivered_l`) is the litres the simulated tap really delivered; the add-on never sees it. What these tests pin is
the policy: the tap is never open without a lease on the controller, the controller's timer closes it at about the target even
when the add-on is gone, the count leaves out water that was already moving, a change in the house's water use stops the fill
instead of over-filling it, a jammed tap gets the branch above it closed, and stop is immediate.

`fill_failures` is the check. Each deliberately broken variant at the end must trip it.
"""
import os, sys, unittest, warnings
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import addon.fill as fill_mod
from addon.service import HuntService, UserError
from addon.store import Store
from huntkit import Config
from tests.test_auto import FakeClock, start_auto

warnings.simplefilter("ignore", ResourceWarning)
TOL_L = 1.5                 # how far from the target, and from the truth, a clean fill may end up (litres)


def make(sources=None):
    clock = FakeClock()
    svc = HuntService(Store(), clock, Config(), key=b"k", public_base_url="https://x.test")
    svc.ensure_demo_household("h", sources={"toilet": 2.0} if sources is None else sources, control=True, outlets=True)
    clock.advance(30)
    svc.tick()
    return svc, clock


def run_fill(target=120.0, sources=None, events=(), dead_at=None, max_s=2000, before=None, outlet="bath", pre_events=()):
    """Start a fill, tick like the server, apply (second, fn) events, and record what a checker needs.

    `pre_events` are (seconds before the request, fn) applied while the house runs for 25 s before the customer asks. A start
    that is refused returns at once with `refused` set; nothing may have opened."""
    svc, clock = make(sources)
    src, hub = svc._source("h"), svc._hub("h")
    o = svc.filler.resolve("h", outlet)
    oid, parent = o["id"], o["parent"]
    if before:
        before(svc, src, hub, clock)
    if pre_events:
        pend = sorted(pre_events, key=lambda e: -e[0])
        for i in range(25):
            while pend and pend[0][0] >= 25 - i:
                pend.pop(0)[1](svc, src, hub, clock)
            clock.advance(1); svc.tick()
        for _, fn in pend:
            fn(svc, src, hub, clock)
        clock.advance(0.5); svc.tick()
    elif before:
        clock.advance(25); svc.tick()
    sent = []
    real = svc._command

    def spy(hid, hunt_id, pipe, want, source, lease_s=None, **k):
        last = svc.store.last_reading_t(hid)
        stale = last is None or clock() - last > svc.cfg.stale_s
        a = real(hid, hunt_id, pipe, want, source, lease_s, **k)
        sent.append(dict(t=clock(), pipe=pipe, want=want, source=source, lease_s=lease_s, ok=a.ok, stale=stale))
        return a
    svc._command = spy
    try:
        p = svc.start_fill("h", outlet, target)
    except UserError as e:
        clock.advance(5); svc.tick()
        return dict(refused=str(e), oid=oid, parent=parent, svc=svc, src=src, hub=hub, clock=clock, sent=sent, target=target,
                    truth=src.delivered_l(oid, clock()), final=None, dead=False, t_request=clock() - 5)
    out = dict(start=p, refused=None, t_request=clock(), oid=oid, parent=parent, no_lease_ticks=0, stop_t=None, stop_closed_t=None, stuck_t=None, backup_closed=False, events=[])
    pending = sorted(events, key=lambda e: e[0])
    t0 = clock()
    for i in range(max_s):
        clock.advance(1)
        while pending and pending[0][0] <= clock() - t0:
            _, fn = pending.pop(0)
            fn(svc, src, hub, clock, out)
        if dead_at is not None and clock() - t0 >= dead_at:
            hub.expire(clock())                     # the add-on is gone: only the controller's own timer runs
        else:
            svc.tick()
        if not hub.offline:
            st = hub.pipes(clock())
            if st[oid].position == "open" and st[oid].lease_left_s is None and oid not in hub.stuck:
                out["no_lease_ticks"] += 1
            if out["stop_t"] is not None and out["stop_closed_t"] is None and not src.outlet_open(oid):
                out["stop_closed_t"] = clock()
            if out["stuck_t"] is not None and st[parent].position == "closed":
                out["backup_closed"] = True
        f = svc.store.latest_fill("h")
        if f["ended"] is not None and (dead_at is None or not src.outlet_open(oid)):
            break
        if dead_at is not None and clock() - t0 > dead_at + 120:
            break
    out.update(svc=svc, src=src, hub=hub, clock=clock, sent=sent, target=target, truth=src.delivered_l(oid, clock()),
               final=svc.store.latest_fill("h"), pres=svc.filler.present(svc.store.latest_fill("h")["id"], clock()),
               dead=dead_at is not None)
    return out


def fill_failures(o):
    f = []
    if o.get("refused") is not None:
        if o["truth"] > 0 or any(s["pipe"] == o["oid"] and s["want"] == "open" for s in o["sent"]):
            f.append("F9 the fill was refused but the tap opened")
        return f
    if o["no_lease_ticks"]:
        f.append(f"F1 the tap was open without a lease on the controller for {o['no_lease_ticks']} s")
    if o["truth"] > o["target"] + TOL_L:
        f.append(f"F2 over-filled: {o['truth']:.1f} litres for a target of {o['target']:.0f}")
    fin = o["final"]
    if fin["ended"] is not None and fin["outcome"] == "done" and abs(fin["state"]["delivered_l"] - o["truth"]) > TOL_L:
        f.append(f"F3 said {fin['state']['delivered_l']:.1f} litres, really {o['truth']:.1f}")
    if fin["ended"] is None and not o["dead"]:
        f.append("F4 the fill never ended")
    renew_stale = [s for s in o["sent"] if s["pipe"] == o["oid"] and s["want"] == "open" and s["stale"] and s["source"] != "fill"]
    if renew_stale:
        f.append(f"F5 extended the tap {len(renew_stale)} times while the meter data was stale")
    if o["stop_t"] is not None and (o["stop_closed_t"] is None or o["stop_closed_t"] - o["stop_t"] > 1.0):
        f.append("F6 stop did not close the tap at once")
    if o["stuck_t"] is not None and not o["backup_closed"]:
        f.append("F7 the tap jammed open and the branch above it was not closed")
    if o["dead"] and o["src"].outlet_open(o["oid"]):
        f.append("F8 the add-on died and the tap stayed open")
    return f


def at(second, fn):
    return (second, fn)


def stop(svc, src, hub, clock, out):
    out["stop_t"] = clock()
    svc.stop_fill("h")


def jam(svc, src, hub, clock, out):
    out["stuck_t"] = clock()
    hub.stuck[out["oid"]] = "open"


class Fill(unittest.TestCase):
    def test_a_clean_fill_stops_at_the_target_and_says_how_much(self):
        o = run_fill()
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "done")
        self.assertAlmostEqual(o["truth"], 120.0, delta=TOL_L)
        self.assertIn("about 120 litres went in", o["pres"]["speech"])

    def test_water_already_moving_is_left_out_and_offered_as_a_leak_hunt(self):
        o = run_fill()
        self.assertIn("already moving somewhere else", o["start"]["speech"])
        self.assertIn("I can find where it's going", o["pres"]["speech"])
        self.assertTrue(o["pres"]["offer_hunt"])
        quiet = run_fill(sources={})
        self.assertNotIn("somewhere else", quiet["pres"]["speech"])
        self.assertFalse(quiet["pres"]["offer_hunt"])
        self.assertEqual(fill_failures(quiet), [])

    def test_the_controllers_timer_is_the_fill_timer_when_the_add_on_dies_near_the_end(self):
        o = run_fill(dead_at=625)                 # 120 L at the simulated 11.4 L/min takes about 631 s, plus the re-check pause
        self.assertEqual(fill_failures(o), [])
        self.assertFalse(o["src"].outlet_open("tub"))
        self.assertAlmostEqual(o["truth"], 120.0, delta=TOL_L)

    def test_the_add_on_dying_early_only_ever_under_fills(self):
        o = run_fill(dead_at=100)
        self.assertEqual(fill_failures(o), [])
        self.assertLess(o["truth"], 120.0)

    def test_someone_else_opening_a_tap_stops_the_fill(self):
        o = run_fill(events=[at(300, lambda svc, src, hub, clock, out: src.set_ambient(1.5, clock()))])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "flow_changed")
        self.assertIn("I'd rather stop than overfill", o["pres"]["speech"])

    def test_water_that_stops_mid_fill_does_not_over_fill_the_bath(self):
        o = run_fill(before=lambda svc, src, hub, clock: src.set_ambient(1.5, clock()),
                     events=[at(300, lambda svc, src, hub, clock, out: src.set_ambient(0.0, clock()))])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "flow_changed")

    def test_lost_meter_data_never_extends_the_tap_and_the_controller_closes_it(self):
        o = run_fill(events=[at(300, lambda svc, src, hub, clock, out: setattr(src, "blackout", True))])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "meter_lost")
        self.assertFalse(o["src"].outlet_open("tub"))
        self.assertIn("by the time it closed", o["pres"]["speech"])

    def test_stop_closes_the_tap_at_once(self):
        o = run_fill(events=[at(100, stop)])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "stopped")

    def test_a_jammed_tap_gets_the_branch_above_it_closed(self):
        o = run_fill(events=[at(100, jam)])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "outlet_stuck")
        self.assertIn("I closed the bathroom branch above it", o["pres"]["speech"])
        t1 = o["truth"]
        o["clock"].advance(300); o["svc"].tick()
        self.assertAlmostEqual(o["src"].delivered_l("tub", o["clock"]()), t1, delta=0.01)      # nothing more went in

    def test_a_controller_that_cannot_be_reached_still_closes_on_its_own_timer(self):
        def down(svc, src, hub, clock, out): hub.offline = True
        o = run_fill(events=[at(100, down)])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "hub_unreachable")
        for _ in range(61):                             # still unreachable: the controller's own timer runs out
            o["clock"].advance(1); o["svc"].tick()
        self.assertFalse(o["src"].outlet_open("tub"))
        self.assertLess(o["src"].delivered_l("tub", o["clock"]()), 120.0)

    def test_turning_off_the_branch_above_ends_the_fill_and_says_why(self):
        o = run_fill(events=[at(150, lambda svc, src, hub, clock, out: svc.set_pipe("h", "bathroom", "off"))])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "branch_closed")

    def test_a_change_just_before_the_request_is_corrected_by_the_re_check(self):
        """Found by probing (not by the fuzzer, which never changed the house in the seconds before a request): someone turning
        off 2.5 L/min a few seconds before asking left the old flow in the baseline and over-filled the bath by 20 to 33 litres."""
        for sb in (3, 6, 8, 14):
            o = run_fill(before=lambda svc, src, hub, clock: src.set_ambient(2.5, clock()),
                         pre_events=[(sb, lambda svc, src, hub, clock: src.set_ambient(0.0, clock()))])
            self.assertEqual(fill_failures(o), [], sb)
            self.assertEqual(o["final"]["outcome"], "done", sb)
            self.assertAlmostEqual(o["truth"], 120.0, delta=TOL_L)
            if sb <= 8:
                self.assertIn("I paused the tap to re-measure", o["pres"]["speech"])
        up = run_fill(pre_events=[(5, lambda svc, src, hub, clock: src.set_ambient(2.5, clock()))])   # the other way: a tap turned on
        self.assertEqual(fill_failures(up), [])
        self.assertAlmostEqual(up["truth"], 120.0, delta=TOL_L)

    def test_the_re_check_pauses_the_tap_once_and_says_so(self):
        svc, clock = make()
        svc.start_fill("h", "bath", 120)
        phases, said = [], set()
        for _ in range(900):
            clock.advance(1); svc.tick()
            f = svc.store.latest_fill("h")
            phases.append(f["state"]["phase"])
            if f["state"]["phase"] == "recheck":
                said.add(svc.filler.present(f["id"], clock())["speech"].split(":")[0])
                self.assertFalse(svc._source("h").outlet_open("tub"))
            if f["ended"] is not None:
                break
        runs = [p for i, p in enumerate(phases) if p == "recheck" and (i == 0 or phases[i - 1] != "recheck")]
        self.assertEqual(len(runs), 1)
        self.assertEqual(said, {"Pausing the bathtub for a few seconds to re-check the count"})
        self.assertEqual(f["outcome"], "done")
        self.assertLess(phases.count("recheck"), 40)

    def test_a_change_during_the_re_check_ends_the_fill_instead_of_resuming(self):
        svc, clock = make()
        svc.start_fill("h", "bath", 120)
        for _ in range(900):
            clock.advance(1); svc.tick()
            if svc.store.latest_fill("h")["state"]["phase"] == "recheck":
                break
        clock.advance(14); svc.tick()
        svc._source("h").set_ambient(2.0, clock())                    # someone opens a tap while the bath is paused
        for _ in range(120):
            clock.advance(1); svc.tick()
            if svc.store.latest_fill("h")["ended"] is not None:
                break
        f = svc.store.latest_fill("h")
        self.assertEqual(f["outcome"], "flow_changed")
        self.assertFalse(svc._source("h").outlet_open("tub"))

    def test_meter_data_lost_during_the_re_check_leaves_the_tap_closed(self):
        o = run_fill(events=[at(195, lambda svc, src, hub, clock, out: setattr(src, "blackout", True))])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "meter_lost_paused")
        self.assertFalse(o["src"].outlet_open("tub"))
        self.assertIn("I've left it closed", o["pres"]["speech"])

    def test_a_controller_lost_during_the_re_check_leaves_the_tap_closed_and_says_so(self):
        svc, clock = make()
        svc.start_fill("h", "bath", 120)
        for _ in range(900):
            clock.advance(1); svc.tick()
            if svc.store.latest_fill("h")["state"]["phase"] == "recheck":
                break
        svc._hub("h").offline = True
        for _ in range(120):
            clock.advance(1); svc.tick()
            if svc.store.latest_fill("h")["ended"] is not None:
                break
        f = svc.store.latest_fill("h")
        self.assertEqual(f["outcome"], "hub_unreachable_paused")
        self.assertFalse(svc._source("h").outlet_open("tub"))
        self.assertIn("couldn't reach your pipe controller to start it again, so it stays closed",
                      svc.filler.present(f["id"], clock())["speech"])

    def test_stop_during_the_re_check_ends_the_fill_with_the_tap_closed(self):
        svc, clock = make()
        svc.start_fill("h", "bath", 120)
        for _ in range(900):
            clock.advance(1); svc.tick()
            if svc.store.latest_fill("h")["state"]["phase"] == "recheck":
                break
        p = svc.stop_fill("h")
        for _ in range(60):
            clock.advance(1); svc.tick()
            if svc.store.latest_fill("h")["ended"] is not None:
                break
        f = svc.store.latest_fill("h")
        self.assertEqual(f["outcome"], "stopped")
        self.assertFalse(svc._source("h").outlet_open("tub"))
        for _ in range(120):                                   # and nothing reopens it later
            clock.advance(1); svc.tick()
        self.assertFalse(svc._source("h").outlet_open("tub"))

    def test_short_fills_do_not_pause(self):
        o = run_fill(target=10.0, outlet="kitchen tap")
        self.assertFalse(o["final"]["state"].get("rechecked"))
        self.assertEqual(fill_failures(o), [])

    def test_a_small_fill_before_any_rate_is_measured(self):
        o = run_fill(target=5)
        self.assertEqual(fill_failures(o), [])
        self.assertAlmostEqual(o["truth"], 5.0, delta=TOL_L)


class EveryTap(unittest.TestCase):
    """The same fill at the kitchen tap and the garden hose: other rates, other branches above them."""

    def test_the_kitchen_tap_and_the_garden_hose_fill_to_the_litre(self):
        for name, litres, oid in (("kitchen tap", 5.0, "ktap"), ("the pot", 2.0, "ktap"), ("garden hose", 30.0, "hose"),
                                  ("plants", 12.0, "hose")):
            o = run_fill(target=litres, outlet=name)
            self.assertEqual(o["oid"], oid)
            self.assertEqual(fill_failures(o), [], name)
            self.assertEqual(o["final"]["outcome"], "done", name)
            self.assertAlmostEqual(o["truth"], litres, delta=TOL_L)
            self.assertEqual(o["src"].delivered_l("tub", o["clock"]()), 0.0)          # no other tap opened

    def test_a_jammed_kitchen_tap_closes_the_kitchen_pipe_not_the_bathroom(self):
        o = run_fill(target=15.0, outlet="kitchen tap", events=[at(20, jam)])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "outlet_stuck")
        st = o["hub"].pipes(o["clock"]())
        self.assertEqual((st["kitchen1"].position, st["bath"].position), ("closed", "open"))
        self.assertIn("kitchen pipe 1", o["pres"]["speech"])

    def test_a_tap_that_reports_open_again_after_closing_gets_the_branch_above_it_closed(self):
        """Found by the fill fuzzer (seed 171): a jam injected while the fill waited for its last reading went unnoticed."""
        svc, clock = make()
        svc.start_fill("h", "kitchen tap", 5)
        hub = svc._hub("h")
        for _ in range(200):
            clock.advance(1); svc.tick()
            if svc.store.latest_fill("h")["state"]["phase"] == "settling":
                break
        self.assertEqual(svc.store.latest_fill("h")["state"]["phase"], "settling")
        hub.stuck["ktap"] = "open"                      # closed a moment ago, now reports open
        for _ in range(60):
            clock.advance(1); svc.tick()
            if svc.store.latest_fill("h")["ended"] is not None:
                break
        f = svc.store.latest_fill("h")
        self.assertEqual(f["outcome"], "outlet_stuck")
        self.assertEqual(hub.pipes(clock())["kitchen1"].position, "closed")
        self.assertIn("I closed the kitchen pipe 1 above it", svc.filler.present(f["id"], clock())["speech"])

    def test_switching_off_the_garden_tap_ends_a_hose_fill(self):
        o = run_fill(target=60.0, outlet="hose", events=[at(60, lambda svc, src, hub, clock, out: svc.set_pipe("h", "garden tap", "off"))])
        self.assertEqual(fill_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "branch_closed")


class StartGuards(unittest.TestCase):
    """Refusals found in review: each would otherwise start a fill that cannot go as promised."""

    def test_no_fill_from_a_tap_whose_branch_is_switched_off(self):
        svc, clock = make()
        r = svc.set_pipe("h", "kitchen pipe 1", "off")
        self.assertNotIn(r.get("kind"), ("choose_pipe", "needs_confirm"))
        clock.advance(2); svc.tick()
        with self.assertRaises(UserError) as e:
            svc.start_fill("h", "kitchen tap", 5)
        self.assertIn("The kitchen pipe 1 is off", str(e.exception))
        self.assertFalse(svc._source("h").outlet_open("ktap"))
        svc.set_pipe("h", "kitchen pipe 1", "on")
        clock.advance(2); svc.tick()
        p = svc.start_fill("h", "kitchen tap", 5)
        self.assertEqual(p["outlet"], "ktap")

    def test_no_fill_while_a_keep_it_closed_question_is_open(self):
        svc, clock = make()
        svc.store.set_offer("h", "garden", clock(), clock() + 300, None, 2.0)   # its lease would reopen the garden pipe mid-fill
        with self.assertRaises(UserError) as e:
            svc.start_fill("h", "bath", 50)
        self.assertIn("Answer that first", str(e.exception))
        self.assertFalse(svc._source("h").outlet_open("tub"))
        svc.store.clear_offer("h", "garden")
        svc.start_fill("h", "bath", 50)

    def test_no_fill_when_the_controller_cannot_be_reached(self):
        svc, clock = make()
        svc._hub("h").offline = True
        with self.assertRaises(UserError) as e:
            svc.start_fill("h", "garden hose", 10)
        self.assertIn("can't reach your pipe controller", str(e.exception))


class Rules(unittest.TestCase):
    def test_the_controller_refuses_to_open_an_outlet_without_a_lease(self):
        svc, clock = make()
        a = svc._hub("h").command("tub", "open", clock(), None)
        self.assertEqual((a.ok, a.error), (False, "lease_required"))
        a = svc._hub("h").command("tub", "open", clock(), 999)
        self.assertTrue(a.ok)
        self.assertLessEqual(svc._hub("h").pipes(clock())["tub"].lease_left_s, 60.0)

    def test_an_outlet_rests_closed_and_a_reboot_closes_it(self):
        svc, clock = make()
        hub = svc._hub("h")
        self.assertEqual(hub.pipes(clock())["tub"].position, "closed")
        hub.command("tub", "open", clock(), 30)
        hub.reboot(clock())
        self.assertEqual(hub.pipes(clock())["tub"].position, "closed")

    def test_no_fill_during_a_hunt_and_no_hunt_during_a_fill(self):
        svc, clock = make()
        start_auto(svc)
        with self.assertRaises(UserError):
            svc.start_fill("h", "bath", 50)
        svc.stop_hunt("h")
        svc.start_fill("h", "bath", 50)
        with self.assertRaises(UserError):
            svc.start_hunt("h", mode="auto", consent=True)

    def test_limits_and_names(self):
        svc, clock = make()
        with self.assertRaises(UserError):
            svc.start_fill("h", "bath", 500)              # more than the tub's limit
        with self.assertRaises(UserError):
            svc.start_fill("h", "bath", 0)
        with self.assertRaises(UserError):
            svc.start_fill("h", "kitchen tap", 25)        # more than the kitchen tap's limit
        with self.assertRaises(UserError) as e:
            svc.start_fill("h", "", None)                 # three taps and no name: ask which
        self.assertIn("Which one should I fill", str(e.exception))
        with self.assertRaises(UserError):
            svc.start_fill("h", "watering can", 10)       # not a tap the controller has
        p = svc.start_fill("h", "bath", None)             # no amount: that tap's saved amount
        self.assertEqual((p["outlet"], p["target_l"]), ("tub", 120.0))
        with self.assertRaises(UserError):
            svc.start_fill("h", "bath", 10)              # already filling
        with self.assertRaises(UserError):
            svc.start_fill("h", "kitchen tap", 2)        # one fill at a time

    def test_no_fill_without_fresh_meter_data(self):
        svc, clock = make()
        svc._source("h").blackout = True
        clock.advance(60); svc.tick()
        with self.assertRaises(UserError) as e:
            svc.start_fill("h", "bath", 50)
        self.assertIn("can't see your water meter", str(e.exception))

    def test_a_branch_kept_closed_after_a_leak_blocks_the_fill(self):
        svc, clock = make()
        svc.store.set_latch("h", "bath", clock(), None, 2.0)
        with self.assertRaises(UserError) as e:
            svc.start_fill("h", "bath", 50)
        self.assertIn("kept closed after a leak", str(e.exception))

    def test_households_without_outlets_are_unchanged(self):
        clock = FakeClock()
        svc = HuntService(Store(), clock, Config(), key=b"k", public_base_url="https://x.test")
        svc.ensure_demo_household("h", control=True)
        self.assertNotIn("tub", svc._hub("h").pipe_ids())
        with self.assertRaises(UserError):
            svc.start_fill("h", "bath", 50)


class BrokenVariants(unittest.TestCase):
    """Each broken fill must trip fill_failures; otherwise a green run of the checks proves nothing."""

    def caught(self, mutant, **kw):
        with mock.patch.object(fill_mod, "MUTANT", mutant):
            return fill_failures(run_fill(**kw))

    def test_a_lease_that_is_not_the_remaining_time(self):
        self.assertTrue(any(x.startswith("F2") for x in self.caught("long_lease", dead_at=625)))

    def test_not_leaving_out_the_water_already_moving(self):
        self.assertTrue(any(x.startswith("F3") or x.startswith("F2") for x in self.caught("no_baseline")))

    def test_not_noticing_a_change(self):
        f = self.caught("ignore_change", before=lambda svc, src, hub, clock: src.set_ambient(1.5, clock()),
                        events=[at(300, lambda svc, src, hub, clock, out: src.set_ambient(0.0, clock()))])
        self.assertTrue(any(x.startswith("F2") for x in f), f)

    def test_extending_the_tap_on_stale_data(self):
        f = self.caught("renew_stale", events=[at(300, lambda svc, src, hub, clock, out: setattr(src, "blackout", True))])
        self.assertTrue(any(x.startswith("F5") for x in f), f)

    def test_no_backup_for_a_jammed_tap(self):
        self.assertTrue(any(x.startswith("F7") for x in self.caught("no_backup", events=[at(100, jam)])))

    def test_no_re_check_after_a_change_just_before_the_request(self):
        f = self.caught("no_recheck", before=lambda svc, src, hub, clock: src.set_ambient(2.5, clock()),
                        pre_events=[(6, lambda svc, src, hub, clock: src.set_ambient(0.0, clock()))])
        self.assertTrue(any(x.startswith("F2") for x in f), f)

    def test_a_stop_that_does_not_close(self):
        self.assertTrue(any(x.startswith("F6") for x in self.caught("no_stop_close", events=[at(100, stop)])))


if __name__ == "__main__":
    unittest.main()
