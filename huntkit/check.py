"""Bounded model checker, systematic fault injection, fuzzing and mutation testing for the hunt engine.

What is checked, at EVERY prefix of EVERY explored schedule (see `properties`):

  P1 soundness    CONFIRMED source/branch claims point at a real source        (<=1 unmodelled flow change)
                  ONLY_SOURCES is never claimed while an unblocked source exists (any schedule)
  P2 clean window every resolved measurement window contains only OPEN, non-leak, same-boot readings
  P3 sanity       counts/durations/flows are non-negative and finite
  P4 safety       if the newest reading shows the safety valve closed -> phase 'aborted', never 'done'
  P5 honest nil   a 'no flow' claim is qualified by a limit >= the sensor floor
  P6 no actuation actions only name known valves; the engine never tells anyone to reopen the safety valve
  P7 idempotence  re-delivering readings shuffled and duplicated yields an identical view (sampled)

The soundness bound is *measured, not assumed*: `tightness()` searches for the smallest number of
unmodelled flow changes that can fool the engine.
"""
from __future__ import annotations

import copy
import itertools
import random
import time
from dataclasses import replace
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .core import Config, Node, ROOT, Topology
from .engine import CONFIRMED, HuntEngine, View
from .sim import VirtualHouse

TICK = 10.0


def check_topology() -> Topology:
    """Small tree used for exhaustive checks: house -> {B -> {T}, K}."""
    return Topology([
        Node("B", "bathroom branch", ROOT, prior=1, how="valve by the door"),
        Node("T", "toilet", "B", prior=2, how="under the cistern"),
        Node("K", "kitchen", ROOT, prior=1, how="under the sink"),
    ])


class World:
    """Ground truth + engine, advanced by discrete events."""

    BURST_LPM = 2.0

    def __init__(self, topo: Topology, sources: Dict[str, float], cfg: Config, *, extra0: float = 0.0,
                 engine_factory: Callable[[Topology, Config], HuntEngine] = HuntEngine):
        self.topo, self.cfg = topo, cfg
        self.sources = dict(sources)
        self.house = VirtualHouse(topo, sources, cfg, t0=1000.0)
        self.house.extra_lpm = extra0
        self.engine = engine_factory(topo, cfg)
        self.generated = []            # every reading the device produced (truth log)
        self.toggles = 1 if extra0 > 0 else 0   # unmodelled flow changes so far (initial 'on' counts as ambient)
        self.toggles = 0
        self.phys: List[Optional[Tuple[str, str]]] = []   # physical action behind each reported step (None = no valve moved)
        self.faults = 0
        self.noncompliant = False
        self.trace: List[str] = []

    def clone(self) -> "World":
        w = copy.copy(self)
        w.house = copy.copy(self.house)
        w.house.closed = set(self.house.closed)
        w.house.sources = dict(self.house.sources)
        e = copy.copy(self.engine)
        e._readings = dict(self.engine._readings)
        e._reports = list(self.engine._reports)
        w.engine = e
        w.generated = list(self.generated)
        w.trace = list(self.trace)
        w.phys = list(self.phys)
        return w

    # ---------------------------------------------------------------- events
    def tick(self, deliver: bool = True) -> None:
        self.house.advance(TICK)
        r = self.house.sample()
        self.generated.append(r)
        if deliver:
            self.engine.ingest(r)

    def apply(self, ev: str) -> None:
        self.trace.append(ev)
        h, eng = self.house, self.engine
        if ev == "W":
            self.tick(); self.tick()
        elif ev == "V":
            self.tick()
        elif ev == "L":
            self.faults += 1
            self.tick(deliver=False)
        elif ev == "Q":
            eng.report(h.t, "quiet")
            self.phys.append(None)
        elif ev[0] in "CO" and ev[1] == ":":
            node = ev[2:]
            was_closed = node in h.closed                 # what the valve was BEFORE this step
            h.advance(3.0)
            if ev[0] == "C":
                h.closed.add(node)
            else:
                h.closed.discard(node)
            h.advance(1.0)
            eng.report(h.t, "close" if ev[0] == "C" else "open", node)
            self.phys.append((node, was_closed))
        elif ev == "Z":                      # the customer really puts the valve back as it was before the last step
            if self.phys:
                act = self.phys.pop()
                if act:
                    node, was_closed = act
                    if was_closed:
                        h.closed.add(node)
                    else:
                        h.closed.discard(node)
            eng.report(h.t, "undo")
        elif ev == "F":
            self.faults += 1
            h.cutoff()
        elif ev == "A":
            self.faults += 1
            h.valve, h.leak = "OPEN", False
        elif ev == "R":
            self.faults += 1
            h.reboot()
        elif ev == "S":                      # customer re-checks and sets the valves exactly as the engine believes
            believed = set(eng.view(h.t).closed)
            h.closed = set(believed)
            eng.report(h.t, "quiet")
            self.phys.append(None)
        elif ev == "X":
            self.faults += 1
            self.toggles += 1
            h.extra_lpm = self.BURST_LPM if h.extra_lpm == 0 else 0.0
        else:
            raise ValueError(ev)

    @property
    def now(self) -> float:
        return self.house.t


# ---------------------------------------------------------------------------------- properties
def _blocked_sources(world: World) -> List[str]:
    return [l for l, q in world.sources.items() if q > 0 and not world.topo.blocked(l, world.house.closed)]


def properties(world: World, view: View, *, soundness: bool = True) -> List[str]:
    v: List[str] = []
    topo, cfg = world.topo, world.cfg
    truth = {l for l, q in world.sources.items() if q > 0}

    if soundness and world.toggles <= 1 and not world.noncompliant:
        for c in view.claims:
            if c.kind == "source_confirmed" and c.node not in truth:
                v.append(f"P1 false accusation of {c.node}")
            if c.kind == "branch_confirmed" and not (set(topo.subtree_leaves(c.node)) & truth):
                v.append(f"P1 false branch {c.node}")
    # completeness claim must be true regardless of toggles, as long as the user did what they reported
    if not world.noncompliant:
        for c in view.claims:
            if c.kind == "only_sources":
                if world.toggles == 0 or True:
                    # zero flow was measured with only the confirmed leaves closed; any other
                    # unblocked source would have produced flow in that window.
                    confirmed = {x.node for x in view.claims if x.kind == "source_confirmed"}
                    missing = truth - confirmed
                    if missing and world.toggles <= 1:
                        # a source that was never confirmed yet flow read zero: only legal if it was
                        # blocked by the closed set at that time -> check against epochs
                        for ep in view.epochs:
                            if ep["idx"] in c.evidence:
                                closed = set(ep["closed"])
                                unblocked = [l for l in missing if not topo.blocked(l, closed)]
                                if unblocked:
                                    v.append(f"P1 only_sources claimed while {unblocked} flows")

    delivered = sorted(world.engine._readings.values(), key=lambda r: r.t)
    byid = {(r.boot_id, r.sample_id): r for r in delivered}
    by_sample = {r.sample_id: r for r in delivered}
    for ep in view.epochs:
        if ep["status"] != "resolved":
            continue
        a, b = by_sample.get(ep["a"]), by_sample.get(ep["b"])
        if not a or not b:
            v.append("P2 window endpoints missing")
            continue
        if a.boot_id != b.boot_id:
            v.append("P2 window spans a reboot")
        inside = [r for r in delivered if a.t <= r.t <= b.t]
        if any(r.boot_id != a.boot_id for r in inside):
            v.append("P2 window contains another boot")
        if any(r.valve != "OPEN" or r.leak for r in inside):
            v.append("P2 window contains a closed/leak reading")
        if not (ep["n"] >= -1e-9 and ep["T"] > 0 and ep["lpm"] >= -1e-9):
            v.append("P3 negative/zero quantities")
    last = delivered[-1] if delivered else None
    if last and (last.valve != "OPEN" or last.leak):
        if view.phase != "aborted":
            v.append("P4 safety valve closed but phase is not aborted")
    if view.phase == "aborted" and "reopen" in view.action.text.lower() and "can't reopen" not in view.action.text.lower():
        v.append("P6 abort text suggests reopening")
    for c in view.claims:
        if c.kind == "no_flow_above" and not (c.hi is not None and c.hi >= cfg.floor_lpm - 1e-9):
            v.append("P5 unqualified no-flow claim")
    a = view.action
    for n in list(a.close) + list(a.open):
        if n not in topo.nodes:
            v.append("P6 unknown valve in action")
    if a.kind not in ("quiet", "close", "open", "set_state", "wait", "done", "abort"):
        v.append("P6 unknown action kind")
    return v


def shuffled_view(world: World, rng: random.Random) -> Tuple[dict, dict]:
    """P7: same readings, scrambled order + duplicates, must give the identical view."""
    eng2 = copy.copy(world.engine)
    eng2._readings = {}
    eng2._reports = list(world.engine._reports)
    rs = list(world.engine._readings.values())
    rs = rs + rs[: len(rs) // 2]
    rng.shuffle(rs)
    for r in rs:
        eng2.ingest(r)
    return world.engine.view(world.now).to_json(), eng2.view(world.now).to_json()


# ---------------------------------------------------------------------------------- exploration
def alphabet(nodes: Sequence[str], faults: str = "LFRAX") -> Tuple[List[str], List[str]]:
    normal = ["W", "V"] + [f"C:{n}" for n in nodes] + [f"O:{n}" for n in nodes]
    return normal, list(faults)


class Stats:
    def __init__(self):
        self.nodes = 0
        self.violations: List[Tuple[List[str], List[str]]] = []
        self.confirmed_reached = 0
        self.only_sources_reached = 0
        self.aborted_reached = 0
        self.max_depth = 0

    def merge(self, o: "Stats") -> None:
        self.nodes += o.nodes
        self.violations += o.violations
        self.confirmed_reached += o.confirmed_reached
        self.only_sources_reached += o.only_sources_reached
        self.aborted_reached += o.aborted_reached
        self.max_depth = max(self.max_depth, o.max_depth)


def explore(topo: Topology, sources: Dict[str, float], cfg: Config, depth: int, *, max_faults: int = 2,
            faults: str = "LFRAX", extra0: float = 0.0, engine_factory=HuntEngine,
            stop_at_violation: int = 50, shuffle_every: int = 997, prefix: Sequence[str] = ("Q",)) -> Stats:
    st = Stats()
    normal, fault_ev = alphabet(list(topo.nodes), faults)
    root = World(topo, sources, cfg, extra0=extra0, engine_factory=engine_factory)
    rng = random.Random(1)
    for ev in prefix:
        root.apply(ev)

    def visit(w: World, d: int) -> None:
        st.nodes += 1
        st.max_depth = max(st.max_depth, len(w.trace))
        view = w.engine.view(w.now)
        bad = properties(w, view)
        if st.nodes % shuffle_every == 0:
            a, b = shuffled_view(w, rng)
            if a != b:
                bad.append("P7 order/duplicate dependence")
        if bad and len(st.violations) < stop_at_violation:
            st.violations.append((list(w.trace), bad))
        if any(c.kind == "source_confirmed" for c in view.claims):
            st.confirmed_reached += 1
        if view.action.outcome == "only_sources":
            st.only_sources_reached += 1
        if view.phase == "aborted":
            st.aborted_reached += 1
        if d == 0:
            return
        for ev in normal:
            w2 = w.clone(); w2.apply(ev); visit(w2, d - 1)
        if w.faults < max_faults:
            for ev in fault_ev:
                if ev == "A" and w.house.valve == "OPEN":
                    continue
                if ev == "F" and w.house.valve != "OPEN":
                    continue
                w2 = w.clone(); w2.apply(ev); visit(w2, d - 1)

    visit(root, depth)
    return st


def skeleton_faults(topo: Topology, sources: Dict[str, float], cfg: Config, script: Sequence[str], *,
                    fault_events: str = "LFRAX", max_faults: int = 2, engine_factory=HuntEngine,
                    extra0: float = 0.0) -> Stats:
    """Take a complete hunt script and inject up to `max_faults` fault events at every position."""
    st = Stats()
    rng = random.Random(2)
    n = len(script)
    positions = range(n + 1)
    for k in range(0, max_faults + 1):
        for pos in itertools.combinations_with_replacement(positions, k):
            for fe in itertools.product(fault_events, repeat=k):
                sched: List[str] = []
                inj = list(zip(pos, fe))
                for i in range(n + 1):
                    for p, f in inj:
                        if p == i:
                            sched.append(f)
                    if i < n:
                        sched.append(script[i])
                w = World(topo, sources, cfg, extra0=extra0, engine_factory=engine_factory)
                ok = True
                for ev in sched:
                    if ev == "A" and w.house.valve == "OPEN":
                        continue
                    if ev == "F" and w.house.valve != "OPEN":
                        continue
                    w.apply(ev)
                    st.nodes += 1
                    view = w.engine.view(w.now)
                    bad = properties(w, view)
                    if bad and len(st.violations) < 50:
                        st.violations.append((list(w.trace), bad))
                    if any(c.kind == "source_confirmed" for c in view.claims):
                        st.confirmed_reached += 1
                    if view.action.outcome == "only_sources":
                        st.only_sources_reached += 1
                    if view.phase == "aborted":
                        st.aborted_reached += 1
                a, b = shuffled_view(w, rng)
                if a != b:
                    st.violations.append((list(w.trace), ["P7 order/duplicate dependence"]))
    return st


def fuzz(topo: Topology, cfg: Config, n_schedules: int, depth: int, seed: int = 0, *, engine_factory=HuntEngine,
         max_toggles_for_soundness: int = 1) -> Stats:
    """Random schedules with random sources, including user mistakes and late reports."""
    rng = random.Random(seed)
    st = Stats()
    leaves = topo.subtree_leaves(ROOT)
    nodes = list(topo.nodes)
    for _ in range(n_schedules):
        sources = {l: rng.choice([1.5, 2.0, 3.0]) for l in leaves if rng.random() < 0.4}
        w = World(topo, sources, cfg, engine_factory=engine_factory)
        w.apply("Q")
        for _step in range(depth):
            r = rng.random()
            if r < 0.40:
                ev = rng.choice(["W", "V", "W"])
            elif r < 0.75:
                ev = rng.choice(["C", "O"]) + ":" + rng.choice(nodes)
            elif r < 0.80:
                ev = "Z"
            else:
                ev = rng.choice(["L", "F", "R", "A", "X", "X"])
            if ev == "A" and w.house.valve == "OPEN":
                continue
            if ev == "F" and w.house.valve != "OPEN":
                continue
            w.apply(ev)
            st.nodes += 1
            view = w.engine.view(w.now)
            bad = properties(w, view)
            if bad and len(st.violations) < 50:
                st.violations.append((list(w.trace), bad))
            if any(c.kind == "source_confirmed" for c in view.claims):
                st.confirmed_reached += 1
            if view.action.outcome == "only_sources":
                st.only_sources_reached += 1
            if view.phase == "aborted":
                st.aborted_reached += 1
        st.max_depth = max(st.max_depth, len(w.trace))
    return st


def tightness(topo: Topology, cfg: Config, script: Sequence[str]) -> Dict[str, object]:
    """How many unmodelled flow changes does it take to fool the engine?

    Truth: NO source at all, but an ambient flow (someone's tap) that is initially ON. We inject k
    toggles of that ambient flow into a full hunt script and ask, for each k:
      * is there a schedule producing a false CONFIRMED source?     (replication A-B-A)
      * is there a schedule producing a false 'only_sources' done?   (replication + proof A-B-A-B)
    Returns the smallest k for each, with a witness.
    """
    out: Dict[str, object] = {}
    n = len(script)
    for label, test in (("false_confirmed", lambda v: any(c.kind == "source_confirmed" for c in v.claims)),
                        ("false_verified", lambda v: v.action.outcome == "only_sources")):
        res = None
        for k in range(0, 5):
            for pos in itertools.combinations_with_replacement(range(n + 1), k):
                sched: List[str] = []
                for i in range(n + 1):
                    for p in pos:
                        if p == i:
                            sched.append("X")
                    if i < n:
                        sched.append(script[i])
                w = World(topo, {}, cfg, extra0=World.BURST_LPM)
                for ev in sched:
                    w.apply(ev)
                if test(w.engine.view(w.now)):
                    res = dict(smallest_fooling_toggles=k, witness=sched)
                    break
            if res:
                break
        out[label] = res if res else "none found up to 4 toggles"
    return out


# ---------------------------------------------------------------------------------- closed-loop (adaptive) checks
def instruction_events(w: World) -> Optional[List[str]]:
    """What a compliant customer does next, given the engine's current instruction. None = hunt over."""
    a = w.engine.view(w.now).action
    if a.kind in ("done", "abort"):
        return None
    if a.kind == "wait":
        return ["V"]
    if a.kind == "quiet":
        return ["S"] if a.why in ("state_mismatch", "state_conflict") else ["Q"]
    if a.kind == "close":
        return [f"C:{a.close[0]}"]
    if a.kind == "open":
        return [f"O:{a.open[0]}"]
    if a.kind == "set_state":
        return [f"C:{n}" for n in a.close] + [f"O:{n}" for n in a.open]
    return None


def _legal(w: World, ev: str) -> bool:
    if ev == "A" and w.house.valve == "OPEN":
        return False
    if ev == "F" and w.house.valve != "OPEN":
        return False
    if ev == "Z" and not w.engine._reports:
        return False
    return True


def adaptive_faults(topo: Topology, sources: Dict[str, float], cfg: Config, *, max_faults: int = 2,
                    fault_events: str = "LFRAXZ", extra0: float = 0.0, max_steps: int = 90,
                    engine_factory=HuntEngine, goal: Optional[Callable[[World, View], bool]] = None) -> Stats:
    """Closed loop: the world always follows the engine's *current* instruction, and up to `max_faults`
    fault events (lost reading, safety cutoff, re-arm, reboot, unmodelled flow toggle, undo-after-doing)
    are injected at EVERY possible position. Unlike a replayed script, this follows the paths the engine
    takes in response to the faults (re-tests, re-measurements, extra verification).

    If `goal` is given, exploration stops at the first state satisfying it (used by tightness searches)."""
    st = Stats()
    st.goal_witness = None
    rng = random.Random(3)

    def observe(w: World) -> View:
        st.nodes += 1
        view = w.engine.view(w.now)
        bad = properties(w, view)
        if bad and len(st.violations) < 50:
            st.violations.append((list(w.trace), bad))
        if any(c.kind == "source_confirmed" for c in view.claims):
            st.confirmed_reached += 1
        if view.action.outcome == "only_sources":
            st.only_sources_reached += 1
        if view.phase == "aborted":
            st.aborted_reached += 1
        if goal and st.goal_witness is None and goal(w, view):
            st.goal_witness = list(w.trace)
        return view

    def run(w: World, used: int, steps: int) -> None:
        if st.goal_witness is not None or steps >= max_steps:
            return
        evs = instruction_events(w)
        if evs is None:
            a, b = shuffled_view(w, rng) if st.nodes % 211 == 0 else ({}, {})
            if a != b:
                st.violations.append((list(w.trace), ["P7 order/duplicate dependence"]))
            return
        w1 = w.clone()
        for ev in evs:
            w1.apply(ev)
            observe(w1)
        run(w1, used, steps + 1)
        if used < max_faults:
            for f in fault_events:
                if not _legal(w, f):
                    continue
                w2 = w.clone()
                w2.apply(f)
                observe(w2)
                for ev in evs:
                    w2.apply(ev)
                    observe(w2)
                run(w2, used + 1, steps + 1)

    root = World(topo, sources, cfg, extra0=extra0, engine_factory=engine_factory)
    observe(root)
    run(root, 0, 0)
    return st


def bad_claims(truth: Dict[str, float]):
    """Goal predicate: a confirmed source that is not real, or 'only these sources' when that is false."""
    real = {l for l, q in truth.items() if q > 0}

    def goal(w: World, v: View) -> Optional[str]:
        conf = {c.node for c in v.claims if c.kind == "source_confirmed"}
        if conf - real:
            return True
        if v.action.outcome == "only_sources" and conf != real:
            return True
        return False
    return goal


def tightness_adaptive(topo: Topology, cfg: Config, truth: Dict[str, float], *, extra0: float = 0.0,
                       k_max: int = 3, fault_events: str = "X") -> Dict[str, object]:
    """Smallest number of unmodelled flow changes that makes the closed-loop engine state something false."""
    for k in range(0, k_max + 1):
        st = adaptive_faults(topo, truth, cfg, max_faults=k, fault_events=fault_events, extra0=extra0, goal=bad_claims(truth))
        if st.goal_witness is not None:
            return dict(smallest_fooling_toggles=k, witness=st.goal_witness, nodes=st.nodes)
    return dict(smallest_fooling_toggles=None, searched_up_to=k_max)


def autopilot_script(topo: Topology, sources: Dict[str, float], cfg: Config, *, max_events: int = 60,
                     extra0: float = 0.0) -> List[str]:
    """The event list a compliant user would produce when following the engine to the end."""
    w = World(topo, sources, cfg, extra0=extra0)
    script: List[str] = []
    for _ in range(max_events):
        v = w.engine.view(w.now)
        a = v.action
        if a.kind in ("done", "abort"):
            break
        evs: List[str] = []
        if a.kind == "wait":
            evs = ["V"]
        elif a.kind == "quiet":
            evs = ["S"] if a.why in ("state_mismatch", "state_conflict") else ["Q"]
        elif a.kind == "close":
            evs = [f"C:{a.close[0]}"]
        elif a.kind == "open":
            evs = [f"O:{a.open[0]}"]
        elif a.kind == "set_state":
            evs = [f"C:{n}" for n in a.close] + [f"O:{n}" for n in a.open]
        for ev in evs:
            w.apply(ev)
            script.append(ev)
    return script
