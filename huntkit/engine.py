"""The hunt engine: a pure function from (readings, user reports) to a verdict.

Design rules (each is a checked invariant, see huntkit/check.py):

  I1  Freshness   - live statements carry the data age; stale data blocks progress.
  I2  Causality   - a measurement only counts after the user's report + settle time, and the
                    window before a report excludes the guard period in which the action
                    may already have happened.
  I3  Safety      - a window containing a CLOSED valve, a leak flag or a reboot is never used.
                    (Otherwise the safety valve closing would look like "found it!".)
  I4  Replication - a source is only CONFIRMED by an alternating triple (A, B, A') whose two
                    contrasts agree. One drop is TENTATIVE at best.
  I5  Honest nil  - "no flow" always carries the smallest flow we could have seen.
  I6  Idempotent  - the result depends only on the *set* of readings, not delivery order/dups.
  I7  No actuation- the engine never emits an instruction to reopen the safety valve.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .core import Config, ClosedSet, Node, Reading, ROOT, Topology

# ----------------------------------------------------------------------------------------
# verdict vocabularies
UNTESTED, TENTATIVE, CONFIRMED, NOT_SOURCE, AMBIGUOUS, INCONSISTENT, UNRESOLVED = (
    "untested", "tentative", "confirmed", "not_source", "ambiguous", "inconsistent", "unresolved",
)


def fmt_lpm(x: float) -> str:
    """One formatter for voice and screen so the two always agree (Amazon CX checklist)."""
    return f"{x:.1f} litres per minute" if x >= 0.05 else "0 litres per minute"


def fmt_lpm_short(x: float) -> str:
    return f"{x:.1f} L/min"


@dataclass
class Meas:
    idx: int
    closed: ClosedSet
    t_start: float
    status: str = "pending"        # pending | resolved | invalid
    reason: str = ""
    a_id: str = ""
    b_id: str = ""
    t_a: float = 0.0
    t_b: float = 0.0
    T: float = 0.0
    n: float = 0.0
    pps: float = 0.0
    lpm: float = 0.0
    var_pps: float = 0.0
    stable: bool = True
    zero: bool = False
    present: bool = False

    def usable(self) -> bool:
        return self.status == "resolved" and self.stable


@dataclass
class Contrast:
    v: str
    o: int            # epoch where v is open
    x: int            # epoch where v is closed
    delta_lpm: float  # open minus closed (expected >= 0)
    sd_lpm: float
    z: float
    kind: str         # drop | contradiction | nochange | ambiguous

    @property
    def later(self) -> int:
        return max(self.o, self.x)


@dataclass
class Claim:
    kind: str
    level: str                  # confirmed | tentative | info | warning
    text: str
    node: Optional[str] = None
    lpm: Optional[float] = None
    lo: Optional[float] = None
    hi: Optional[float] = None
    evidence: List[int] = field(default_factory=list)


@dataclass
class Action:
    kind: str                   # quiet | close | open | set_state | wait | done | abort
    text: str
    close: List[str] = field(default_factory=list)
    open: List[str] = field(default_factory=list)
    eta_s: Optional[float] = None
    why: str = ""
    outcome: str = ""           # for done/abort


@dataclass
class View:
    phase: str
    action: Action
    claims: List[Claim]
    epochs: List[dict]
    verdicts: Dict[str, str]
    closed: List[str]
    data_age_s: Optional[float]
    stale: bool
    flow_now_lpm: Optional[float]
    steps_done: int
    est_steps_left: Optional[int]

    def to_json(self) -> dict:
        return dict(
            phase=self.phase,
            action=asdict(self.action),
            claims=[asdict(c) for c in self.claims],
            epochs=self.epochs,
            verdicts=self.verdicts,
            closed=self.closed,
            data_age_s=self.data_age_s,
            stale=self.stale,
            flow_now_lpm=self.flow_now_lpm,
            steps_done=self.steps_done,
            est_steps_left=self.est_steps_left,
        )


@dataclass(frozen=True)
class Report:
    seq: int
    t: float
    kind: str                  # quiet | close | open | undo
    node: Optional[str] = None


def _entropy(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


class HuntEngine:
    def __init__(self, topo: Topology, cfg: Config = Config()):
        self.topo = topo
        self.cfg = cfg
        self._readings: Dict[Tuple[str, str], Reading] = {}
        self._reports: List[Report] = []

    # ------------------------------------------------------------------ inputs
    def ingest(self, r: Reading) -> bool:
        """Idempotent. Returns True if the reading was new."""
        key = (r.boot_id, r.sample_id)
        if key in self._readings:
            return False
        self._readings[key] = r
        return True

    def report(self, t: float, kind: str, node: Optional[str] = None) -> Report:
        if kind not in ("quiet", "close", "open", "undo"):
            raise ValueError(kind)
        if kind in ("close", "open") and node not in self.topo.nodes:
            raise ValueError(f"unknown node {node}")
        rep = Report(seq=len(self._reports), t=t, kind=kind, node=node)
        self._reports.append(rep)
        return rep

    @property
    def readings(self) -> List[Reading]:
        return sorted(self._readings.values(), key=lambda r: (r.t, r.uptime_ms, r.sample_id))

    @property
    def reports(self) -> List[Report]:
        return list(self._reports)

    # ------------------------------------------------------------------ epochs
    def _effective_reports(self) -> List[Report]:
        stack: List[Report] = []
        for rep in sorted(self._reports, key=lambda r: r.seq):
            if rep.kind == "undo":
                if stack:
                    stack.pop()
            else:
                stack.append(rep)
        return stack

    def _epochs(self) -> Tuple[List[Tuple[float, ClosedSet, Report]], ClosedSet]:
        """Time-ordered measurement epochs, one per state change the customer reported, plus the valves the customer
        reported closed BEFORE the hunt started (the baseline must be taken with every valve open, so a 'quiet' while
        any of those is still closed does not start the hunt).

        'undo' is NOT a retroactive edit. The customer may really have turned the valve and later turned
        it back, so every epoch since the undone step ran under a physical state we cannot vouch for:
        those epochs are marked `tainted` (never measured), and a `restore` epoch starts at the undo.
        """
        eps: List[Tuple[float, ClosedSet, Report]] = []
        closed: set = set()
        started = False
        stack: List[Tuple[FrozenSet[str], Optional[int], Report]] = []   # (state before, first epoch index, report)
        for rep in sorted(self._reports, key=lambda r: r.seq):
            if rep.kind == "undo":
                if not stack:
                    continue
                prev, first, orig = stack.pop()
                if first is None:                      # the undone report started no epoch; it may have moved a pre-start valve
                    closed = set(prev)
                    continue
                if first == 0:                         # undoing the very start of the hunt: begin again later
                    del eps[:]
                    started = False
                    closed = set()
                    continue
                for j in range(first, len(eps)):
                    t, c, r = eps[j]
                    if r.kind != "tainted":
                        eps[j] = (t, c, Report(-1, t, "tainted", r.node))
                closed = set(prev)
                eps.append((rep.t, frozenset(closed), Report(rep.seq, rep.t, "restore", orig.node)))
                continue
            before = frozenset(closed)
            made = None
            if rep.kind == "quiet":
                if not started and closed:
                    pass                               # a valve is still shut: this is not yet a baseline
                else:
                    started = True
                    made = len(eps)
                    eps.append((rep.t, frozenset(closed), rep))
            elif not started:
                if rep.kind == "close":                # reported before the hunt began: remember it, so we never
                    closed.add(rep.node)               # take a "baseline" with a valve the customer told us is shut
                elif rep.kind == "open":
                    closed.discard(rep.node)
            elif rep.kind == "close":
                if rep.node not in closed:
                    closed.add(rep.node)
                    made = len(eps)
                    eps.append((rep.t, frozenset(closed), rep))
            elif rep.kind == "open":
                if rep.node in closed:
                    closed.discard(rep.node)
                    made = len(eps)
                    eps.append((rep.t, frozenset(closed), rep))
            stack.append((before, made, rep))
        # epochs must be time-ordered; reports are sequence-ordered. A report whose time is
        # earlier than its predecessor is clamped (clock skew) so windows never overlap.
        fixed: List[Tuple[float, ClosedSet, Report]] = []
        last = -1e18
        for t, c, rep in eps:
            t = max(t, last + 1e-6)
            fixed.append((t, c, rep))
            last = t
        return fixed, (frozenset(closed) if not started else frozenset())

    # ------------------------------------------------------------------ measurement
    def _measure_epoch(self, i: int, eps, readings: List[Reading], now: float) -> Meas:
        cfg = self.cfg
        t0, closed, _rep = eps[i]
        m = Meas(idx=i, closed=closed, t_start=t0)
        if _rep.kind == "tainted":
            m.status, m.reason = "invalid", "undone_step"
            return m
        start = t0 + cfg.settle_s + cfg.slack_s
        is_last = i == len(eps) - 1
        # The user may act at any moment after we speak and report within ~act_guard_s. For the
        # newest epoch we therefore only trust data older than now - guard, which makes a verdict
        # issued now stay valid when the next report arrives (no retroactive invalidation).
        end = (now - cfg.act_guard_s) if is_last else eps[i + 1][0] - cfg.act_guard_s
        cand = [r for r in readings if start <= r.t <= end]
        if not cand:
            m.status = "pending" if is_last else "invalid"
            m.reason = "waiting_for_data" if is_last else "no_window_before_next_step"
            return m
        # split into contiguous same-boot segments
        segs: List[List[Reading]] = []
        for r in cand:
            if segs and segs[-1][-1].boot_id == r.boot_id:
                segs[-1].append(r)
            else:
                segs.append([r])
        last_reason = "waiting_for_data" if is_last else "window_too_short"
        for si, seg in enumerate(segs):
            if any((r.valve != "OPEN") or r.leak for r in seg):
                m.status = "invalid"
                m.reason = "device_safety_valve_closed"          # I3
                return m
            if len(seg) < 2:
                last_reason = "reboot_truncated" if si < len(segs) - 1 else last_reason
                continue
            # counters/clocks must be sane within the segment
            sane = True
            for p, q in zip(seg, seg[1:]):
                if q.out_total_l < p.out_total_l - 1e-9 or q.uptime_ms <= p.uptime_ms:
                    sane = False
                    break
            a, b = seg[0], seg[-1]
            T = (b.uptime_ms - a.uptime_ms) / 1000.0
            if (not sane) or abs((b.t - a.t) - T) > cfg.clock_tol_s:
                m.status = "invalid"
                m.reason = "counter_or_clock_inconsistent"
                return m
            if T < cfg.min_window_s:
                last_reason = "window_too_short" if si == len(segs) - 1 else "reboot_truncated"
                continue
            n = (b.out_total_l - a.out_total_l) * cfg.ppl
            pps = n / T
            m.status = "resolved"
            m.a_id, m.b_id, m.t_a, m.t_b, m.T, m.n, m.pps = a.sample_id, b.sample_id, a.t, b.t, T, n, pps
            m.lpm = cfg.pps_to_lpm(pps)
            m.var_pps = cfg.dispersion * max(pps, 0.0) / T + (cfg.rel_sd * max(pps, 0.0)) ** 2 + 1.0 / (12.0 * T * T)
            m.zero = n <= cfg.zero_pulses(T)
            m.present = n >= cfg.present_pulses(T)
            # stability: no interval may deviate wildly from the window mean (interference,
            # an intermittent source, or the action landing inside the window)
            stable = True
            if pps > 0:
                for p, q in zip(seg, seg[1:]):
                    Ti = (q.uptime_ms - p.uptime_ms) / 1000.0
                    ni = (q.out_total_l - p.out_total_l) * cfg.ppl
                    sd = math.sqrt(cfg.dispersion * pps * Ti + 1.0 / 6.0 + 1.0)
                    if abs(ni - pps * Ti) / sd > cfg.stable_z:
                        stable = False
                        break
            m.stable = stable
            if not stable:
                m.reason = "unstable_flow"
            return m
        # no segment qualified
        if is_last:
            m.status = "pending"
            m.reason = last_reason
        else:
            m.status = "invalid"
            m.reason = last_reason
        return m

    # ------------------------------------------------------------------ contrasts
    def _contrast(self, o: Meas, x: Meas, v: str) -> Contrast:
        cfg = self.cfg
        dpps = o.pps - x.pps
        sd = math.sqrt(o.var_pps + x.var_pps)
        z = dpps / sd if sd > 0 else 0.0
        d_lpm, sd_lpm = cfg.pps_to_lpm(dpps), cfg.pps_to_lpm(sd)
        if z >= cfg.z and d_lpm >= cfg.min_effect_lpm:
            kind = "drop"
        elif z <= -cfg.z and -d_lpm >= cfg.min_effect_lpm:
            kind = "contradiction"
        elif (d_lpm + cfg.z * sd_lpm) < cfg.min_drop_frac * max(o.lpm, 0.0) and \
             (-d_lpm + cfg.z * sd_lpm) < cfg.min_drop_frac * max(o.lpm, 0.0) and o.lpm >= cfg.floor_lpm:
            kind = "nochange"
        else:
            kind = "ambiguous"
        return Contrast(v=v, o=o.idx, x=x.idx, delta_lpm=d_lpm, sd_lpm=sd_lpm, z=z, kind=kind)

    def _contrasts(self, ms: List[Meas]) -> Dict[str, List[Contrast]]:
        out: Dict[str, List[Contrast]] = {v: [] for v in self.topo.nodes}
        use = [m for m in ms if m.usable()]
        for i, a in enumerate(use):
            for b in use[i + 1:]:
                diff = a.closed ^ b.closed
                if len(diff) != 1:
                    continue
                (v,) = tuple(diff)
                if v in b.closed:        # a open, b closed
                    out[v].append(self._contrast(a, b, v))
                else:                    # a closed, b open
                    out[v].append(self._contrast(b, a, v))
        return out

    def _consistent(self, c1: Contrast, c2: Contrast) -> bool:
        cfg = self.cfg
        diff = abs(c1.delta_lpm - c2.delta_lpm)
        tol = cfg.z * math.sqrt(c1.sd_lpm ** 2 + c2.sd_lpm ** 2) + 0.25 * max(c1.delta_lpm, c2.delta_lpm)
        return diff <= tol

    def _verdicts(self, ms: List[Meas]):
        cons = self._contrasts(ms)
        verdicts: Dict[str, str] = {}
        info: Dict[str, dict] = {}
        for v, cs in cons.items():
            cs = sorted(cs, key=lambda c: (c.later, c.o, c.x))
            confirmed = None
            for c1 in cs:
                for c2 in cs:
                    if c1 is c2 or c1.kind != "drop" or c2.kind != "drop":
                        continue
                    # alternating triple: the two contrasts share the middle epoch and
                    # the outer epochs lie on opposite sides of it in time
                    shared = {c1.o, c1.x} & {c2.o, c2.x}
                    if len(shared) != 1:
                        continue
                    mid = next(iter(shared))
                    outer1 = (c1.o if c1.x == mid else c1.x)
                    outer2 = (c2.o if c2.x == mid else c2.x)
                    if outer1 == outer2:
                        continue
                    if not (min(outer1, outer2) < mid < max(outer1, outer2)):
                        continue
                    if self._consistent(c1, c2):
                        confirmed = (c1, c2, mid)
                        break
                if confirmed:
                    break
            n_bad = sum(1 for c in cs if c.kind in ("ambiguous", "contradiction"))
            if confirmed:
                verdicts[v] = CONFIRMED
                c1, c2, mid = confirmed
                info[v] = dict(delta=0.5 * (c1.delta_lpm + c2.delta_lpm),
                               sd=0.5 * math.hypot(c1.sd_lpm, c2.sd_lpm),
                               evidence=sorted({c1.o, c1.x, c2.o, c2.x}))
            elif not cs:
                verdicts[v] = UNTESTED
                info[v] = {}
            else:
                last = cs[-1]
                info[v] = dict(delta=last.delta_lpm, sd=last.sd_lpm, evidence=[last.o, last.x], n_bad=n_bad,
                               bound=max(0.0, last.delta_lpm + self.cfg.z * last.sd_lpm))
                if n_bad >= 3:
                    verdicts[v] = UNRESOLVED
                elif last.kind == "drop":
                    verdicts[v] = TENTATIVE
                elif last.kind == "nochange":
                    verdicts[v] = NOT_SOURCE
                elif last.kind == "contradiction":
                    verdicts[v] = INCONSISTENT
                else:
                    verdicts[v] = AMBIGUOUS
        return verdicts, info, cons

    def _reconcile(self, ms: List[Meas], verdicts, info, cons):
        """Belief check: a confirmed leaf must show up in every epoch where it is believed unblocked.

        If an epoch reads far BELOW what the confirmed leaks alone would produce, our idea of which valves are
        closed is wrong (a missed undo, an extra valve closed, an intermittent source). Such epochs are
        discarded, so no claim can rest on them. Iterates because discarding can change what is confirmed.
        """
        cfg, topo = self.cfg, self.topo
        any_change = False
        for _ in range(4):
            conf = {v: info[v] for v, s in verdicts.items() if s == CONFIRMED and topo.is_leaf(v)}
            if not conf:
                break
            changed = False
            for m in ms:
                if not m.usable():
                    continue
                exp = sum(max(0.0, inf["delta"] - cfg.z * inf["sd"]) for leaf, inf in conf.items()
                          if not topo.blocked(leaf, m.closed))
                if exp <= 0.0:
                    continue
                if m.lpm + cfg.z * cfg.pps_to_lpm(math.sqrt(m.var_pps)) < exp:
                    m.status, m.reason = "invalid", "state_mismatch"
                    changed = True
            if not changed:
                break
            any_change = True
            verdicts, info, cons = self._verdicts(ms)
        return verdicts, info, cons, any_change

    def _conflict(self, a: Meas, b: Meas) -> bool:
        """Two readings of the SAME believed valve state that cannot both be right."""
        cfg = self.cfg
        sd = math.sqrt(a.var_pps + b.var_pps)
        d = abs(a.pps - b.pps)
        return sd > 0 and d / sd >= cfg.z and cfg.pps_to_lpm(d) >= cfg.min_effect_lpm

    def _same_state(self, ms: List[Meas]) -> bool:
        """Recurrence check: the same valve state must read the same every time we look.

        When two epochs with identical closed sets disagree, either the water use changed or a valve is not where
        we believe it is. Whichever it is, the NEWEST reading only counts once an older one backs it up; until then
        it is withdrawn and the customer is asked to re-check. When the newest reading is backed up, the older
        readings that disagree with it are superseded. The very first (baseline) epoch is never withdrawn.
        """
        groups: Dict[ClosedSet, List[Meas]] = {}
        for m in ms:
            if m.usable():
                groups.setdefault(m.closed, []).append(m)
        changed = False
        for g in groups.values():
            if len(g) < 2:
                continue
            last = g[-1]
            bad = [m for m in g[:-1] if self._conflict(m, last)]
            if not bad:
                continue
            if len(g) - len(bad) >= 2:
                for m in bad:
                    if m.idx != 0:
                        m.status, m.reason = "invalid", "superseded"
                        changed = True
            else:
                last.status, last.reason = "invalid", "state_conflict"
                changed = True
        return changed

    def _settle(self, ms: List[Meas]):
        verdicts, info, cons = self._verdicts(ms)
        for _ in range(8):
            ch1 = self._same_state(ms)
            if ch1:
                verdicts, info, cons = self._verdicts(ms)
            verdicts, info, cons, ch2 = self._reconcile(ms, verdicts, info, cons)
            if not (ch1 or ch2):
                break
        return verdicts, info, cons

    # ------------------------------------------------------------------ planning
    def _candidate_leaves(self, verdicts: Dict[str, str], found: List[str]) -> List[str]:
        topo = self.topo
        L = set(topo.subtree_leaves(ROOT))
        for v, s in verdicts.items():
            if s == NOT_SOURCE:
                L -= set(topo.subtree_leaves(v))
        if not found:
            for v, s in verdicts.items():
                if s == CONFIRMED and not topo.is_leaf(v):
                    L &= set(topo.subtree_leaves(v))      # single-source assumption until the final check
        else:
            L -= set(found)
        return sorted(L)

    def _next_test(self, verdicts: Dict[str, str], closed: ClosedSet, found: List[str]) -> Optional[Tuple[str, str]]:
        """Pick the next valve to operate: finish verifications first, then the most informative
        test per unit effort. Returns (mode, node) with mode in {'close','open'} or None."""
        topo = self.topo
        free = lambda v: not any(a in closed for a in topo.ancestors(v))
        for v in topo.operable():                                  # 1. replication of a drop
            if verdicts[v] == TENTATIVE and free(v):
                return ("open" if v in closed else "close", v)
        for v in topo.operable():                                  # 2. re-measure unclear nodes
            if verdicts[v] in (AMBIGUOUS, INCONSISTENT) and free(v):
                return ("open" if v in closed else "close", v)
        L = self._candidate_leaves(verdicts, found)
        if not L:
            return None
        tot = sum(topo.nodes[l].prior for l in L) or 1e-9
        best = None
        for v in topo.operable():
            if verdicts[v] in (NOT_SOURCE, CONFIRMED, UNRESOLVED, TENTATIVE, AMBIGUOUS, INCONSISTENT):
                continue
            if v in closed or not free(v):
                continue
            sub = [l for l in topo.subtree_leaves(v) if l in L]
            if not sub:
                continue
            p = sum(topo.nodes[l].prior for l in sub) / tot
            size = len(topo.subtree_leaves(v))
            score = _entropy(p) / max(topo.nodes[v].cost, 1e-9) + 1e-3 * (1.0 - size / 100.0)
            key = (score, -size)
            if best is None or key > best[0]:
                best = (key, v)
        return ("close", best[1]) if best else None

    def _confirmed_leaves(self, verdicts: Dict[str, str]) -> List[str]:
        return [v for v, s in verdicts.items() if s == CONFIRMED and self.topo.is_leaf(v)]

    # ------------------------------------------------------------------ the view
    def view(self, now: float) -> View:
        cfg, topo = self.cfg, self.topo
        readings = self.readings
        eps, pre_closed = self._epochs()
        ms = [self._measure_epoch(i, eps, readings, now) for i in range(len(eps))]
        verdicts, info, cons = self._settle(ms)
        last_r = readings[-1] if readings else None
        age = (now - last_r.t) if last_r else None
        stale = age is None or age > cfg.stale_s
        flow_now = None
        if len(readings) >= 2 and not stale:
            a, b = readings[-2], readings[-1]
            if a.boot_id == b.boot_id and b.uptime_ms > a.uptime_ms and b.out_total_l >= a.out_total_l:
                flow_now = cfg.pps_to_lpm((b.out_total_l - a.out_total_l) * cfg.ppl / ((b.uptime_ms - a.uptime_ms) / 1000.0))
        claims: List[Claim] = []
        epochs_json = [self._meas_json(m) for m in ms]
        closed_now: ClosedSet = eps[-1][1] if eps else frozenset()

        def done(outcome: str, text: str) -> View:
            return View("done", Action("done", text, outcome=outcome), claims, epochs_json, verdicts,
                        sorted(closed_now), age, stale, flow_now, len(eps), 0)

        def abort(reason: str, text: str) -> View:
            claims.append(Claim("aborted", "warning", text))
            return View("aborted", Action("abort", text, outcome=reason), claims, epochs_json, verdicts,
                        sorted(closed_now), age, stale, flow_now, len(eps), None)

        # I3/I7: the device's own safety valve always wins.
        if last_r and (last_r.valve != "OPEN" or last_r.leak):
            return abort("device_safety_valve_closed",
                         "Your main safety valve closed by itself. I can't reopen it from here; "
                         "inspect the plumbing and re-arm it at the device, then we can continue.")

        if not eps and pre_closed:
            return View("idle", self._set_state([], sorted(pre_closed),
                                                "Every valve has to be open for the first measurement.", "open_first"),
                        claims, epochs_json, verdicts, sorted(pre_closed), age, stale, flow_now, 0, None)

        if not eps:
            return View("idle", Action("quiet", "Turn off everything that uses water — taps, washing machine, "
                                       "cistern fills — but leave every isolation valve open. Tell me when that's done.",
                                       why="baseline"), claims, epochs_json, verdicts, [], age, stale, flow_now, 0, None)

        if len(eps) > cfg.max_epochs:
            return abort("too_many_steps", "This is taking more steps than it should, which usually means water is being "
                         "used or a valve isn't where I think it is. I'd rather stop than guess. Let's start again "
                         "when the house is quiet.")

        base = ms[0]
        # --- baseline claims
        if base.status == "invalid":
            return View("baseline", Action("quiet", "I couldn't get a clean reading. Keep everything off and tell me "
                                           "when you're ready, and I'll measure again.", why=base.reason),
                        claims, epochs_json, verdicts, sorted(closed_now), age, stale, flow_now, len(eps), None)
        if base.status == "pending":
            return View("baseline", self._wait(now, eps, ms, "Measuring the quiet baseline."), claims,
                        epochs_json, verdicts, sorted(closed_now), age, stale, flow_now, len(eps), None)
        T = base.T
        limit = max(cfg.floor_lpm, cfg.pps_to_lpm(cfg.zero_pulses(T) / T))
        if base.zero:
            claims.append(Claim("no_flow_above", "confirmed",
                                f"With everything off, I measured no water moving above {fmt_lpm(limit)} "
                                f"over {T:.0f} seconds. That is the smallest flow your sensor can show me, "
                                f"so I can't say anything about slower drips.",
                                lpm=0.0, hi=limit, evidence=[0]))
            return done("no_flow_above_limit", claims[-1].text)
        if not base.present or base.lpm < cfg.floor_lpm:
            claims.append(Claim("below_floor", "info",
                                f"Something small may be moving ({base.lpm:.2f} L/min), but that is below what "
                                f"your flow sensor reads reliably ({fmt_lpm_short(cfg.floor_lpm)}). "
                                f"I won't guess; an overnight meter test is the right tool for drips.",
                                lpm=base.lpm, evidence=[0]))
            return done("below_sensor_floor", claims[-1].text)
        if not base.stable:
            claims.append(Claim("unstable_baseline", "info",
                                "The flow is starting and stopping, which looks like a cyclic source such as a "
                                "refilling cistern. A short hunt could be fooled by that, so I won't run one.",
                                lpm=base.lpm, evidence=[0]))
            return done("unstable_baseline", claims[-1].text)
        claims.append(Claim("flow_present", "confirmed",
                            f"With everything off, {fmt_lpm(base.lpm)} is still flowing "
                            f"(measured over {base.T:.0f} seconds).", lpm=base.lpm, evidence=[0]))

        # --- per-node claims
        for v, s in verdicts.items():
            d = info[v].get("delta")
            sd = info[v].get("sd", 0.0)
            ev = info[v].get("evidence", [])
            lab = topo.label(v)
            leaf = topo.is_leaf(v)
            if s == CONFIRMED:
                txt = (f"Confirmed: {fmt_lpm(d)} is flowing behind the {lab} valve. Closing it stopped that flow and "
                       f"reopening it brought it back, with matching size both times. The flow is isolated to the water "
                       f"behind that valve." if leaf else
                       f"Confirmed: {fmt_lpm(d)} is flowing behind the {lab} valve, that is, somewhere in that branch. "
                       f"Closing it stopped the flow and reopening it brought it back.")
                claims.append(Claim("source_confirmed" if leaf else "branch_confirmed", "confirmed", txt, node=v,
                                    lpm=d, lo=d - topo_z(cfg) * sd, hi=d + topo_z(cfg) * sd, evidence=ev))
            elif s == TENTATIVE:
                claims.append(Claim("drop_observed", "tentative",
                                    f"Flow dropped by {fmt_lpm(d)} when the {lab} was closed. "
                                    f"I won't call that a finding yet: I need to see it come back.",
                                    node=v, lpm=d, evidence=ev))
            elif s == NOT_SOURCE:
                claims.append(Claim("not_source", "info",
                                    f"Closing the {lab} changed the flow by no more than "
                                    f"{fmt_lpm(info[v].get('bound', 0.0))}, so the water isn't going through it.",
                                    node=v, hi=info[v].get("bound"), evidence=ev))
            elif s == INCONSISTENT:
                claims.append(Claim("inconsistent", "warning",
                                    f"The flow went up after the {lab} was closed, which can't be the valve. "
                                    f"Someone may have used water. Let's repeat that step.", node=v, evidence=ev))
            elif s == AMBIGUOUS:
                claims.append(Claim("ambiguous", "warning",
                                    f"I couldn't tell whether the {lab} mattered; the reading was too noisy. "
                                    f"Let's measure again.", node=v, evidence=ev))
            elif s == UNRESOLVED:
                claims.append(Claim("unresolved", "warning",
                                    f"I couldn't get a clear answer for the {lab} after several tries.", node=v))

        # latest epoch must be resolved before the plan advances
        last_m = ms[-1]
        if last_m.status == "pending":
            return View("measuring", self._wait(now, eps, ms, "Measuring the effect of your last step."), claims,
                        epochs_json, verdicts, sorted(closed_now), age, stale, flow_now, len(eps), self._est_left(verdicts))
        if last_m.status == "invalid" and last_m.reason in ("state_mismatch", "state_conflict"):
            names = " and ".join(f"the {topo.label(c)}" for c in sorted(closed_now))
            want = (f"only {names} closed" if closed_now else "every valve open")
            lead = ("The flow doesn't match what I'd expect with" if last_m.reason == "state_mismatch" else
                    "This reads differently from the last time the valves were in this same position, with")
            return View("measuring", Action("quiet", f"{lead} {want}. Please check "
                        f"that's exactly how the valves are, with everything else open and nothing running, "
                        f"then tell me when it's right.", why=last_m.reason),
                        claims, epochs_json, verdicts, sorted(closed_now), age, stale, flow_now, len(eps), self._est_left(verdicts))
        if last_m.status == "invalid" or not last_m.stable:
            why = last_m.reason or "unstable_flow"
            return View("measuring", Action("wait", "That measurement wasn't clean "
                        f"({why.replace('_', ' ')}). Keep the valves as they are and make sure nobody uses "
                        "water; I'll re-measure. Say 'undo' if you didn't actually change anything.",
                        why=why, eta_s=cfg.settle_s + cfg.cadence_s + cfg.min_window_s),
                        claims, epochs_json, verdicts, sorted(closed_now), age, stale, flow_now, len(eps), self._est_left(verdicts))

        # An unclear contrast gets more power simply by waiting: the window keeps growing.
        if last_m.T < cfg.max_window_s and any(
                c.kind == "ambiguous" and last_m.idx in (c.o, c.x) for cs in cons.values() for c in cs):
            return View("measuring", Action("wait", "The change is too small to call yet; collecting a little more "
                        "data so I don't guess.", why="need_more_power",
                        eta_s=min(cfg.cadence_s, cfg.max_window_s - last_m.T)),
                        claims, epochs_json, verdicts, sorted(closed_now), age, stale, flow_now, len(eps),
                        self._est_left(verdicts))

        # --- completeness check
        leaves = self._confirmed_leaves(verdicts)
        target = frozenset(leaves)
        if leaves:
            # The completeness proof must come AFTER the replication: the culprit is closed once more
            # (what you'd do to stop the leak anyway) and the rest of the house reads zero. That makes
            # the evidence an A-B-A-B sequence, which needs three unmodelled flow changes to fake.
            after = -1
            for l in leaves:
                open_ev = [i for i in info[l].get("evidence", []) if l not in ms[i].closed]
                after = max(after, max(open_ev) if open_ev else -1)
            final = [m for m in ms if m.usable() and m.closed == target and m.idx > after]
            if final:
                fm = final[-1]
                if fm.zero:
                    names = " and ".join(topo.label(l) for l in sorted(leaves))
                    claims.append(Claim("only_sources", "confirmed",
                                        f"With only the {names} isolated and everything else open, no water moves "
                                        f"above {fmt_lpm(max(cfg.floor_lpm, cfg.pps_to_lpm(cfg.zero_pulses(fm.T) / fm.T)))}. "
                                        f"Nothing else is running continuously above that limit. "
                                        + (f"Leave the {names} valve closed until it's repaired."
                                           if len(leaves) == 1 else
                                           "Leave those valves closed until they're repaired."),
                                        lpm=0.0, evidence=[fm.idx]))
                    return done("only_sources", claims[-1].text)
                if fm.present and last_m.idx >= fm.idx and last_m.closed == target:
                    claims.append(Claim("remainder", "warning",
                                        f"Even with the {', '.join(topo.label(l) for l in sorted(leaves))} isolated, "
                                        f"{fmt_lpm(fm.lpm)} is still flowing, so there is another source.",
                                        lpm=fm.lpm, evidence=[fm.idx]))
            elif last_m.closed != target:
                close = sorted(target - closed_now)
                opn = sorted(closed_now - target)
                return View("final_check", self._set_state(close, opn,
                            "Final check: isolate only that valve and open everything else. If the flow reads zero, "
                            "it is the only source above what my sensor can read."), claims, epochs_json, verdicts,
                            sorted(closed_now), age, stale, flow_now, len(eps), 1)

        step = self._next_test(verdicts, closed_now, leaves)
        if step is None:
            if leaves:
                claims.append(Claim("partial", "warning",
                                    "I found a confirmed source, but some flow remains that I can't place at any "
                                    "valve you've told me about.", evidence=[]))
                return done("partial", claims[-1].text)
            rem = ms[-1].lpm
            txt = (f"None of the valves you told me about stopped the flow ({fmt_lpm(rem)} still moving). So the "
                   "water is escaping where those valves don't reach: pipework upstream of them, between them, or "
                   "a fixture with no valve of its own. That's a job for a plumber, and I'll write up the evidence.")
            claims.append(Claim("upstream_remainder", "confirmed", txt, lpm=rem, evidence=[ms[-1].idx]))
            return done("upstream_remainder", txt)
        mode, k = step
        lab = topo.label(k)
        how = f" ({topo.nodes[k].how})" if topo.nodes[k].how else ""
        why = {"close": "test", "open": "verify"}[mode]
        verb = "Close" if mode == "close" else "Open"
        extra = ""
        if verdicts[k] == TENTATIVE:
            extra = " I'm checking the flow comes back so I don't blame the wrong thing."
        act = Action(mode, f"{verb} the {lab} valve{how}, then tell me.{extra}", close=[k] if mode == "close" else [],
                     open=[k] if mode == "open" else [], why=why)
        phase = "verifying" if verdicts[k] == TENTATIVE else "searching"
        return View(phase, act, claims, epochs_json, verdicts, sorted(closed_now), age, stale, flow_now, len(eps),
                    self._est_left(verdicts))

    # ------------------------------------------------------------------ helpers
    def _set_state(self, close: List[str], opn: List[str], why: str, code: str = "final_check") -> Action:
        parts = []
        if close:
            parts.append("close " + " and ".join(f"the {self.topo.label(c)}" for c in close))
        if opn:
            parts.append("open " + " and ".join(f"the {self.topo.label(c)}" for c in opn))
        text = ("Please " + ", then ".join(parts) + ", then tell me. " if parts else "") + why
        return Action("set_state", text.strip(), close=close, open=opn, why=code)

    def _wait(self, now: float, eps, ms: List[Meas], why: str) -> Action:
        cfg = self.cfg
        t0 = eps[-1][0]
        m = ms[-1]
        start = t0 + cfg.settle_s + cfg.slack_s
        remaining = max(0.0, start - now) + cfg.cadence_s + cfg.min_window_s
        if m.status == "pending" and m.t_a:
            remaining = max(0.0, cfg.min_window_s - (m.t_b - m.t_a)) + cfg.cadence_s
        return Action("wait", f"{why} About {int(round(remaining))} seconds.", eta_s=remaining, why=why)

    def _est_left(self, verdicts: Dict[str, str]) -> Optional[int]:
        topo = self.topo
        left = 0
        scope = ROOT
        while True:
            kids = [k for k in topo.children(scope) if verdicts[k] != NOT_SOURCE]
            if not kids:
                break
            confirmed = [k for k in kids if verdicts[k] == CONFIRMED and not topo.is_leaf(k)]
            if confirmed:
                scope = confirmed[0]
                continue
            untested = [k for k in kids if verdicts[k] in (UNTESTED, AMBIGUOUS, INCONSISTENT)]
            tentative = [k for k in kids if verdicts[k] == TENTATIVE]
            left += len(tentative)
            if untested:
                # expected additional probes: half of the remaining candidates, rounded up, plus the open/close pair
                left += max(1, math.ceil(len(untested) / 2)) + 1
            break
        return left + 1

    def _meas_json(self, m: Meas) -> dict:
        return dict(idx=m.idx, closed=sorted(m.closed), status=m.status, reason=m.reason, T=round(m.T, 3),
                    n=round(m.n, 3), lpm=round(m.lpm, 3), a=m.a_id, b=m.b_id, stable=m.stable,
                    zero=m.zero, present=m.present, t_start=m.t_start)


def topo_z(cfg: Config) -> float:
    return cfg.z
