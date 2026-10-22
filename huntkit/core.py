"""Core types for huntkit: readings, plumbing topology, configuration.

huntkit is a small, dependency-free engine for *sensor-verified guided hunts*:
an agent asks a human to change the state of a physical system (close a valve),
and only states a conclusion if a measurement stream supports it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

ROOT = "house"


@dataclass(frozen=True)
class Config:
    """Tunable assumptions. Every value here must be confirmed on the real rig."""

    ppl: float = 360.0          # pulses per litre (assumed; calibrate! see docs/BENCH.md)
    stale_s: float = 35.0       # data older than this is treated as stale
    cadence_s: float = 10.0     # telemetry cadence (latest sample every ~10 s)
    settle_s: float = 3.0       # wait after a reported action before measuring
    slack_s: float = 2.0        # clock slack between user report and sensor clock
    act_guard_s: float = 5.0    # readings this close before a report may already include the action
    min_window_s: float = 9.0   # shortest measurement window we will decide on (>= one interval)
    z: float = 3.09             # one-sided ~1e-3
    dispersion: float = 1.0     # Poisson-equivalent variance factor on pulse counts (CALIBRATE)
    rel_sd: float = 0.05        # window-to-window relative flow variability (CALIBRATE on bench)
    max_window_s: float = 40.0  # stop waiting for more power after this long
    max_epochs: int = 40        # a hunt this long has stopped converging: stop and say so
    floor_lpm: float = 1.0      # smallest flow the sensor class reports reliably (MEASURE ON BENCH)
    noise_pps: float = 0.02     # tolerated spurious pulses per second at zero flow (MEASURE ON BENCH)
    min_drop_frac: float = 0.5   # smallest share of the flow we claim to be able to exclude
    stable_z: float = 4.0       # per-interval deviation that marks a window as unstable
    clock_tol_s: float = 3.0    # tolerated wall-clock vs uptime disagreement inside a window

    # derived helpers -------------------------------------------------
    @property
    def min_effect_lpm(self) -> float:
        return 0.5 * self.floor_lpm

    def pps_to_lpm(self, pps: float) -> float:
        return pps * 60.0 / self.ppl

    def lpm_to_pps(self, lpm: float) -> float:
        return lpm * self.ppl / 60.0

    def zero_pulses(self, T: float) -> int:
        """Largest pulse count over T seconds still consistent with 'no flow'."""
        import math
        return max(1, math.ceil(2.0 * self.noise_pps * T))

    def present_pulses(self, T: float) -> int:
        return self.zero_pulses(T) + 3


@dataclass(frozen=True)
class Reading:
    """One telemetry sample from the flow sensor."""

    sample_id: str
    boot_id: str
    uptime_ms: int
    t: float                    # estimated wall-clock time of the sample, seconds
    out_total_l: float          # cumulative outlet volume since boot (litres)
    valve: str = "OPEN"         # OPEN | CLOSED | UNKNOWN
    leak: bool = False          # firmware leak_detected flag


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    parent: str = ROOT
    prior: float = 1.0          # relative prior that a continuous flow lives here (leaves)
    cost: float = 1.0           # relative effort to operate this valve
    how: str = ""               # where to find it, e.g. "under the cistern"
    # --- how the customer names it (used by voice; the hunt engine ignores all of these)
    room: str = ""              # "kitchen"
    number: int = 0             # 1, 2 ... when a room has several pipes ("kitchen pipe 2"); 0 = unnumbered
    aliases: Tuple[str, ...] = ()   # extra names the customer uses ("sink", "dishwasher")
    protect: bool = False       # never switched by software (feeds a water heater, a sprinkler ...)


class Topology:
    """A rooted tree of isolation points. ROOT is the house and is not operable."""

    def __init__(self, nodes: Sequence[Node]):
        self.nodes: Dict[str, Node] = {}
        for n in nodes:
            if n.id == ROOT:
                raise ValueError("'house' is reserved for the root")
            if n.id in self.nodes:
                raise ValueError(f"duplicate node {n.id}")
            self.nodes[n.id] = n
        for n in nodes:
            if n.parent != ROOT and n.parent not in self.nodes:
                raise ValueError(f"unknown parent {n.parent} for {n.id}")
        self._children: Dict[str, List[str]] = {ROOT: []}
        for n in nodes:
            self._children.setdefault(n.id, [])
        for n in nodes:
            self._children[n.parent].append(n.id)
        # cycle check
        for n in nodes:
            seen = set()
            cur = n.id
            while cur != ROOT:
                if cur in seen:
                    raise ValueError("cycle in topology")
                seen.add(cur)
                cur = self.nodes[cur].parent

    def children(self, v: str) -> List[str]:
        return list(self._children.get(v, []))

    def is_leaf(self, v: str) -> bool:
        return not self._children.get(v)

    def operable(self) -> List[str]:
        return list(self.nodes)

    def subtree_leaves(self, v: str) -> List[str]:
        if v == ROOT:
            out: List[str] = []
            for c in self.children(ROOT):
                out.extend(self.subtree_leaves(c))
            return out
        if self.is_leaf(v):
            return [v]
        out = []
        for c in self.children(v):
            out.extend(self.subtree_leaves(c))
        return out

    def ancestors(self, v: str) -> List[str]:
        out = []
        cur = v
        while cur != ROOT:
            cur = self.nodes[cur].parent
            if cur != ROOT:
                out.append(cur)
        return out

    def blocked(self, leaf: str, closed: Iterable[str]) -> bool:
        cs = set(closed)
        return leaf in cs or any(a in cs for a in self.ancestors(leaf))

    def label(self, v: str) -> str:
        return self.nodes[v].label if v in self.nodes else v

    def to_json(self) -> List[dict]:
        return [
            dict(id=n.id, label=n.label, parent=n.parent, prior=n.prior, cost=n.cost, how=n.how,
                 room=n.room, number=n.number, aliases=list(n.aliases), protect=n.protect)
            for n in self.nodes.values()
        ]

    @staticmethod
    def from_json(rows: Sequence[dict]) -> "Topology":
        out = []
        for r in rows:
            r = dict(r)
            r["aliases"] = tuple(str(a) for a in (r.get("aliases") or ()))   # rows saved before pipes had names
            out.append(Node(**r))
        return Topology(out)


ClosedSet = FrozenSet[str]
