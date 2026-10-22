import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from huntkit import Config, HuntEngine, Sim, VirtualHouse, demo_topology  # noqa: E402
from huntkit.core import Reading  # noqa: E402


def make(sources, cfg=None, **kw):
    cfg = cfg or Config()
    topo = demo_topology()
    house = VirtualHouse(topo, sources, cfg, **kw)
    eng = HuntEngine(topo, cfg)
    return topo, cfg, house, eng, Sim(house, eng)


def kinds(view):
    return {c.kind for c in view.claims}


class ValvesReportedBeforeTheBaseline(unittest.TestCase):
    """Regression: found by closed-loop fault injection. A valve the customer reported shut BEFORE the baseline used
    to be ignored, so a later 'quiet' became a baseline with that branch cut off and its leak was never seen - and the
    engine then claimed 'nothing else is leaking'."""

    def test_a_shut_valve_blocks_the_baseline_until_it_is_reported_open(self):
        _, _, house, eng, sim = make({"toilet": 2.0, "kitchen1": 1.5})
        sim.run(25)
        house.closed.add("bath")
        eng.report(house.t, "close", "bath")              # reported while the engine still waits for the quiet baseline
        eng.report(house.t, "quiet")
        v = eng.view(house.t)
        self.assertEqual(v.action.kind, "set_state")
        self.assertEqual(v.action.open, ["bath"])
        self.assertEqual(v.action.why, "open_first")
        self.assertEqual(v.epochs, [])                    # no baseline was taken
        self.assertFalse(any(c.kind in ("flow_present", "only_sources") for c in v.claims))
        house.closed.discard("bath")
        sim.user("open", "bath")                          # now it is open again, as reported
        sim.user("quiet")
        out = sim.autopilot()
        self.assertEqual(out.action.outcome, "only_sources")
        self.assertEqual({c.node for c in out.claims if c.kind == "source_confirmed"}, {"toilet", "kitchen1"})

    def test_undoing_the_pre_start_close_forgets_it(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        eng.report(house.t, "close", "bath")
        eng.report(house.t, "undo")
        eng.report(house.t, "quiet")
        v = eng.view(house.t)
        self.assertEqual(v.phase, "baseline")

    def test_the_found_counterexample_is_now_safe(self):
        from huntkit.check import World, check_topology, properties
        sched = ["Q", "V", "V", "V", "Z", "C:B", "Q", "V", "V", "V", "C:B", "V", "V", "V", "V", "Z", "C:K", "V", "V", "V", "O:K",
                 "V", "V", "V", "C:K", "V", "V", "V"]
        w = World(check_topology(), {"T": 2.0, "K": 1.5}, Config())
        for ev in sched:
            w.apply(ev)
            self.assertEqual(properties(w, w.engine.view(w.now)), [], ev)


class HappyPaths(unittest.TestCase):
    def test_single_source_found_and_proven_alone(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        v = sim.autopilot()
        self.assertEqual(v.action.outcome, "only_sources")
        self.assertEqual({c.node for c in v.claims if c.kind == "source_confirmed"}, {"toilet"})

    def test_source_in_last_place_still_found(self):
        _, _, house, eng, sim = make({"garden": 3.0})
        sim.run(25)
        v = sim.autopilot()
        self.assertEqual(v.action.outcome, "only_sources")
        self.assertEqual({c.node for c in v.claims if c.kind == "source_confirmed"}, {"garden"})

    def test_two_sources_both_found(self):
        _, _, house, eng, sim = make({"shower": 1.5, "kitchen1": 2.5})
        sim.run(25)
        v = sim.autopilot()
        self.assertEqual(v.action.outcome, "only_sources")
        self.assertEqual({c.node for c in v.claims if c.kind == "source_confirmed"}, {"shower", "kitchen1"})

    def test_no_leak_claim_is_qualified_by_a_limit(self):
        cfg = Config()
        _, _, house, eng, sim = make({}, cfg)
        sim.run(25)
        v = sim.autopilot()
        self.assertEqual(v.action.outcome, "no_flow_above_limit")
        c = [c for c in v.claims if c.kind == "no_flow_above"][0]
        self.assertGreaterEqual(c.hi, cfg.floor_lpm)
        self.assertIn("slower drips", c.text)

    def test_below_sensor_floor_is_not_guessed(self):
        _, _, house, eng, sim = make({"toilet": 0.3})
        sim.run(25)
        v = sim.autopilot()
        self.assertEqual(v.action.outcome, "below_sensor_floor")
        self.assertFalse(any(c.kind in ("source_confirmed", "drop_observed") for c in v.claims))

    def test_noisy_flow_never_accuses_the_wrong_valve(self):
        wrong = 0
        for seed in range(40):
            _, _, house, eng, sim = make({"toilet": 2.0}, seed=seed, noise=0.10)
            sim.run(random.Random(seed).uniform(10, 40))
            v = sim.autopilot()
            conf = {c.node for c in v.claims if c.kind == "source_confirmed"}
            if conf - {"toilet"}:
                wrong += 1
        self.assertEqual(wrong, 0)


class HonestyAndSafety(unittest.TestCase):
    def test_single_drop_is_tentative_not_a_finding(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        sim.user("quiet")
        sim.run(40)
        sim.user("close", "toilet")
        sim.run(40)
        v = eng.view(house.t)
        self.assertEqual(v.verdicts["toilet"], "tentative")
        self.assertNotIn("source_confirmed", kinds(v))
        self.assertEqual(v.action.kind, "open")           # asks to see the flow come back

    def test_firmware_cutoff_aborts_and_never_looks_like_a_find(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        sim.user("quiet")
        sim.run(35)
        sim.user("close", "bath")
        house.cutoff()                       # the safety valve closes at the very same moment
        sim.run(40)
        v = eng.view(house.t)
        self.assertEqual(v.phase, "aborted")
        self.assertFalse(any(c.kind in ("source_confirmed", "branch_confirmed") for c in v.claims))
        self.assertIn("can't reopen", v.action.text)

    def test_cutoff_then_rearm_does_not_poison_windows(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        sim.user("quiet")
        sim.run(35)
        sim.user("close", "shower")
        house.cutoff()
        sim.run(20)
        house.valve, house.leak = "OPEN", False   # re-armed at the device
        sim.run(40)
        v = eng.view(house.t)
        shower_epoch = [e for e in v.epochs if e["closed"] == ["shower"]][0]
        self.assertNotEqual(shower_epoch["status"], "resolved")      # the CLOSED reading invalidates it
        self.assertNotIn("drop_observed", kinds(v))

    def test_reboot_mid_hunt_never_differences_across_boots(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        sim.user("quiet")
        sim.run(22)
        house.reboot()
        sim.run(40)
        v = eng.view(house.t)
        base = v.epochs[0]
        self.assertEqual(base["status"], "resolved")
        self.assertAlmostEqual(base["lpm"], 2.0, delta=0.2)
        self.assertGreaterEqual(base["n"], 0)

    def test_ambient_tap_that_stops_is_not_blamed_on_a_valve(self):
        # Truth: nothing leaks. Someone's tap runs during the baseline and is turned off just as
        # the first valve is closed. Closing then reopening that valve must NOT produce a finding.
        _, _, house, eng, sim = make({}, )
        house.extra_lpm = 2.0
        sim.run(25)
        sim.user("quiet")
        sim.run(35)
        house.extra_lpm = 0.0
        sim.user("close", "toilet")
        sim.run(40)
        sim.user("open", "toilet")
        sim.run(60)
        v = eng.view(house.t)
        self.assertFalse(any(c.kind in ("source_confirmed", "branch_confirmed") for c in v.claims))

    def test_cyclic_source_is_declined_not_guessed(self):
        _, _, house, eng, sim = make({"toilet": 3.0})
        sim.run(25)
        sim.user("quiet")
        # toilet refills in bursts: on for 6 s, off for 14 s
        for _ in range(4):
            house.sources["toilet"] = 3.0
            sim.run(6)
            house.sources["toilet"] = 0.0
            sim.run(14)
        v = eng.view(house.t)
        self.assertNotIn("source_confirmed", kinds(v))

    def test_stale_data_is_flagged(self):
        _, cfg, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        house.advance(120)
        v = eng.view(house.t)
        self.assertTrue(v.stale)
        self.assertIsNone(v.flow_now_lpm)

    def test_undo_removes_the_step(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        sim.user("quiet")
        sim.run(35)
        sim.user("close", "toilet", physical=False)
        eng.report(house.t, "undo")
        v = eng.view(house.t)
        self.assertEqual(len(eng._effective_reports()), 1)

    def test_no_instruction_ever_names_a_safety_valve(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        seen = []
        for _ in range(120):
            v = eng.view(house.t)
            seen.append(v.action.text.lower())
            if v.action.kind in ("done", "abort"):
                break
            sim.autopilot(max_s=2)
        self.assertFalse(any("re-arm" in t and "open" in t and "can't" not in t for t in seen))


class Idempotence(unittest.TestCase):
    def test_delivery_order_and_duplicates_do_not_matter(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        sim.autopilot()
        ref = eng.view(house.t).to_json()
        rs = list(eng._readings.values())
        for seed in range(25):
            rng = random.Random(seed)
            e2 = HuntEngine(eng.topo, eng.cfg)
            shuffled = rs + rng.sample(rs, k=len(rs) // 2)
            rng.shuffle(shuffled)
            for r in shuffled:
                e2.ingest(r)
            e2._reports = list(eng._reports)
            self.assertEqual(e2.view(house.t).to_json(), ref)

    def test_ingest_reports_whether_new(self):
        _, _, house, eng, sim = make({})
        r = Reading("s1", "b", 1000, 5.0, 0.0)
        self.assertTrue(eng.ingest(r))
        self.assertFalse(eng.ingest(r))


class Wording(unittest.TestCase):
    def test_spoken_and_displayed_text_are_the_same_string(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        v = sim.autopilot()
        js = v.to_json()
        self.assertEqual([c["text"] for c in js["claims"]], [c.text for c in v.claims])


class UndoIsNotRetroactive(unittest.TestCase):
    """Found by fuzzing: 'undo' after the customer really did the step must not rewrite what the sensor saw."""

    def test_fuzz_counterexample_no_false_accusation(self):
        from huntkit.check import World, check_topology
        topo, cfg = check_topology(), Config()
        sched = ["Q", "W", "C:B", "Z", "V", "W", "L", "C:T", "W", "C:B", "Z", "L", "W", "O:K", "O:T", "C:T", "C:K",
                 "W", "L", "W", "Z", "O:T", "V", "W"]
        for flow in (1.5, 2.0, 3.0):
            w = World(topo, {"K": flow}, cfg)
            for ev in sched:
                w.apply(ev)
            v = w.engine.view(w.now)
            self.assertFalse(any(c.kind == "source_confirmed" and c.node == "T" for c in v.claims), flow)

    def test_undone_step_taints_its_epochs_and_restores_the_state(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        sim.user("quiet")
        sim.run(35)
        sim.user("close", "toilet")
        sim.run(20)
        sim.user("close", "kitchen1")                # customer really closes it, then changes their mind
        sim.run(20)
        house.closed.discard("kitchen1")             # ...reopens it physically
        eng.report(house.t, "undo")
        sim.run(40)
        v = eng.view(house.t)
        kinds_ = [e["status"] + ":" + e["reason"] for e in v.epochs]
        self.assertIn("invalid:undone_step", kinds_)
        self.assertEqual(v.epochs[-1]["closed"], ["toilet"])       # state restored to what it was before the step
        self.assertEqual(v.epochs[-1]["status"], "resolved")

    def test_undoing_the_quiet_report_restarts_the_hunt(self):
        _, _, house, eng, sim = make({"toilet": 2.0})
        sim.run(25)
        sim.user("quiet")
        sim.run(5)
        eng.report(house.t, "undo")
        self.assertEqual(eng.view(house.t).phase, "idle")


class SameStateMustReadTheSame(unittest.TestCase):
    """Found by closed-loop fault injection: two undos, the second without the valve really going back."""

    PRE = ["Q", "V", "V", "V", "C:B", "V", "V", "V", "O:B", "V", "V", "V", "C:T", "V", "V", "V", "O:T", "V", "V", "V",
           "C:T", "V", "V", "V", "C:K", "V", "V", "V", "O:K", "V", "V", "Z", "V", "V"]

    def _world(self):
        from huntkit.check import World, check_topology
        w = World(check_topology(), {"T": 2.0, "K": 1.5}, Config())
        for ev in self.PRE:
            w.apply(ev)
        return w

    def test_undo_that_did_not_physically_happen_is_not_believed(self):
        w = self._world()
        w.engine.report(w.house.t, "undo")             # says 'undo', but the kitchen valve is still shut
        for ev in ["V"] * 4:
            w.apply(ev)
        v = w.engine.view(w.now)
        self.assertNotEqual(v.action.outcome, "only_sources")
        self.assertEqual(v.action.why, "state_conflict")
        self.assertEqual(v.action.kind, "quiet")
        self.assertEqual(v.epochs[-1]["reason"], "state_conflict")

    def test_recheck_supersedes_the_contradicted_reading(self):
        w = self._world()
        w.engine.report(w.house.t, "undo")
        for ev in ["V"] * 4:
            w.apply(ev)
        w.apply("S")                                   # customer puts the valves where the engine believes they are
        for ev in ["V"] * 6:
            w.apply(ev)
        v = w.engine.view(w.now)
        st = [e["status"] + ":" + e["reason"] for e in v.epochs]
        self.assertIn("invalid:superseded", st)
        self.assertEqual(v.epochs[-1]["status"], "resolved")
        self.assertGreater(v.epochs[-1]["lpm"], 1.0)    # the kitchen leak is visible again

    def test_two_real_undos_just_keep_searching(self):
        w = self._world()
        w.apply("Z")
        for ev in ["V"] * 6:
            w.apply(ev)
        v = w.engine.view(w.now)
        self.assertNotEqual(v.action.outcome, "only_sources")
        self.assertFalse(any(c.kind == "source_confirmed" and c.node == "K" for c in v.claims))

    def test_a_hunt_that_never_converges_stops(self):
        from huntkit.check import World, check_topology
        w = World(check_topology(), {"T": 2.0}, Config(max_epochs=6))
        w.apply("Q")
        for _ in range(12):
            for ev in ["V", "V", "V", "C:B", "V", "V", "V", "O:B"]:
                w.apply(ev)
        v = w.engine.view(w.now)
        self.assertEqual(v.action.outcome, "too_many_steps")


class RebootGuardIsBackedByTheCounterAndClockChecks(unittest.TestCase):
    """Mutant M4 (no reboot guard) survives the property checker. The reason is derived in tools/m4_analysis.py and pinned
    here: a window across a reboot is refused by the counter / uptime / clock checks unless boot 1 was younger than
    `clock_tol_s` at the window's first reading, and where it is accepted under constant flow the rate is exact."""

    @staticmethod
    def _tool():
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), "..", "tools", "m4_analysis.py")
        spec = importlib.util.spec_from_file_location("m4_analysis", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _engines(self):
        from huntkit.core import Node, Topology
        from huntkit.engine import Report
        from huntkit.mutants import NoRebootGuard
        topo = Topology([Node("a", "a")])
        cfg = Config()
        t0 = 1000.0
        return cfg, HuntEngine(topo, cfg), NoRebootGuard(topo, cfg), [(t0, frozenset(), Report(0, t0, "quiet"))], t0

    def test_an_ordinary_reboot_is_refused_by_the_counter_check_even_without_the_guard(self):
        tool = self._tool()
        cfg, real, m4, eps, t0 = self._engines()
        now = t0 + 60
        rs = tool.build(B1=t0 - 100, B2=t0 + 20, boot1=(5.0, None), boot2=(5.0, None), q1=2, q2=2, jitter=0.0, horizon=now)
        glued = m4._measure_epoch(0, eps, rs, now)
        self.assertEqual((glued.status, glued.reason), ("invalid", "counter_or_clock_inconsistent"))
        honest = real._measure_epoch(0, eps, rs, now)
        self.assertEqual(honest.status, "resolved")
        self.assertAlmostEqual(honest.lpm, 2.0, places=9)

    def test_across_a_reboot_the_mutant_accepts_only_a_boot_loop_and_then_measures_the_true_rate(self):
        import itertools
        tool = self._tool()
        cfg, real, m4, eps, t0 = self._engines()
        accepted = 0
        for A1, off, boot1, boot2, (q1, q2), jit in itertools.product(
                (0.5, 1, 2.9, 3.5, 8, 20), (6, 9, 14, 30), ((0.5, None), (2.5, 2.0)), ((0.5, 1.0), (5.0, None), (10.0, None)),
                ((2, 2), (3, 3), (0, 0), (2, 0), (0, 2)), (0.0, 2.0, -2.0)):
            now = t0 + 45
            rs = tool.build(t0 + off - A1, t0 + off, boot1, boot2, q1, q2, jit, now)
            if len(rs) < 2:
                continue
            m = m4._measure_epoch(0, eps, rs, now)
            if m.status != "resolved":
                continue
            ra = next(r for r in rs if r.sample_id == m.a_id)
            rb = next(r for r in rs if r.sample_id == m.b_id)
            if ra.boot_id == rb.boot_id:
                continue
            accepted += 1
            self.assertLessEqual(ra.uptime_ms / 1000.0, cfg.clock_tol_s, "boot 1 must be a boot loop (younger than clock_tol_s)")
            self.assertGreater(rb.uptime_ms, ra.uptime_ms)
            if q1 == q2:
                self.assertAlmostEqual(m.lpm, float(q1), places=9, msg="constant flow is measured exactly across the glued reboot")
        self.assertGreater(accepted, 0, "the grid must reach the boot-loop corner, otherwise this test proves nothing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
