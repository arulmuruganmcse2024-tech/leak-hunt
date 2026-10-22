"""Sensor sources for the add-on.

VirtualSource  - a virtual house driven lazily by the wall clock. This is what the demo, the tests and every judge run;
                 nothing else is needed to run the whole project.
A real flow sensor needs an optional adapter that this build does not include (docs/DISCLOSURE.md); nothing else depends on it.
A source returns `Reading`s (huntkit.core.Reading): cumulative litres, uptime, boot id, valve state, leak flag.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set

from huntkit.core import Config, Reading, Topology
from huntkit.sim import VirtualHouse


class VirtualSource:
    name = "virtual"
    simulated = True

    def __init__(self, topo: Topology, sources: Dict[str, float], cfg: Config, clock: Callable[[], float], *,
                 warmup_s: float = 90.0, cadence_s: float = 10.0):
        self.cfg, self.clock, self.cadence_s = cfg, clock, cadence_s
        now = clock()
        self.house = VirtualHouse(topo, sources, cfg, t0=now - warmup_s, boot_id="virtual-boot")
        self._next = now - warmup_s + cadence_s       # the first poll() returns the warm-up samples
        self._buf: List[Reading] = []                 # samples produced while a valve moved; poll() hands them over
        self.blackout = False                         # challenge mode only: the sensor's link is down, the house keeps flowing
        # Outlets (a bath or garden tap the add-on can fill from). Their flow is added to the house's extra flow, as a delta,
        # so anything else that sets extra flow keeps its share. Ground truth: the litres each outlet really delivered.
        self.outlets: Dict[str, dict] = {}
        self._outlet_open: Set[str] = set()
        self._outlet_lpm = 0.0
        self._ambient_lpm = 0.0
        self._delivered: Dict[str, float] = {}
        self._acc_t = now

    def _sample_until(self, now: float) -> None:
        while self._next <= now:
            self.house.advance(self._next - self.house.t)
            self._buf.append(self.house.sample())
            self._next += self.cadence_s

    def poll(self, now: float) -> List[Reading]:
        self._sample_until(now)
        out, self._buf = self._buf, []
        return [] if self.blackout else out          # samples taken during a blackout are lost, not delayed

    def set_closed(self, node: str, closed: bool, at: float) -> None:
        """Physically move a valve at time `at` (physics is advanced to `at` first, so earlier samples keep the old state)."""
        self._sample_until(at)
        self.house.advance(max(0.0, at - self.house.t))
        self._accrue(at)
        if closed:
            self.house.closed.add(node)
        else:
            self.house.closed.discard(node)
        self._reapply()

    # ------------------------------------------------------------------ outlets (simulation only)
    def add_outlet(self, oid: str, lpm: float, parent: str) -> None:
        self.outlets[oid] = dict(lpm=float(lpm), parent=parent)
        self._delivered.setdefault(oid, 0.0)

    def _rate(self, oid: str) -> float:
        o = self.outlets[oid]
        if oid not in self._outlet_open or self.house.valve != "OPEN":
            return 0.0
        if o["parent"] in self.house.topo.nodes and self.house.topo.blocked(o["parent"], self.house.closed):
            return 0.0
        return o["lpm"]

    def _accrue(self, t: float) -> None:
        if t > self._acc_t:
            for oid in self.outlets:
                self._delivered[oid] += self._rate(oid) * (t - self._acc_t) / 60.0
            self._acc_t = t

    def _reapply(self) -> None:
        new = sum(self._rate(o) for o in self.outlets)
        self.house.extra_lpm += new - self._outlet_lpm
        self._outlet_lpm = new

    def set_outlet(self, oid: str, open_: bool, at: float) -> None:
        """Open or close an outlet at time `at` (the hub calls this; only the hub moves an outlet)."""
        self._sample_until(at)
        self.house.advance(max(0.0, at - self.house.t))
        self._accrue(at)
        if open_:
            self._outlet_open.add(oid)
        else:
            self._outlet_open.discard(oid)
        self._reapply()

    def outlet_open(self, oid: str) -> bool:
        return oid in self._outlet_open

    def delivered_l(self, oid: str, t: float) -> float:
        """Ground truth: litres the outlet has really delivered up to time t. Tests and fuzzers only; the add-on never sees it."""
        return self._delivered.get(oid, 0.0) + (self._rate(oid) * max(0.0, t - self._acc_t) / 60.0 if oid in self.outlets else 0.0)

    def set_ambient(self, lpm: float, at: float) -> None:
        """Someone else's tap (challenge mode, fuzzers): extra flow that is not an outlet the add-on controls."""
        self._sample_until(at)
        self.house.advance(max(0.0, at - self.house.t))
        self._accrue(at)
        self.house.extra_lpm += float(lpm) - self._ambient_lpm
        self._ambient_lpm = float(lpm)

    def on_user_report(self, kind: str, node: Optional[str], now: float, lag_s: float = 4.0) -> List[Reading]:
        """The virtual 'human' turns the valve `lag_s` seconds before saying 'done' (like a real person)."""
        if kind in ("close", "open") and node:
            self.set_closed(node, kind == "close", now - lag_s)
        return self.poll(now)

    def reset_valves(self, now: float) -> List[Reading]:
        """Demo convenience: a new hunt in the simulated house starts with every valve open again."""
        self._sample_until(now)
        self.house.advance(max(0.0, now - self.house.t))
        self._accrue(now)
        self.house.closed.clear()
        self._reapply()
        return self.poll(now)

    def set_sources(self, sources: Dict[str, float]) -> None:
        self.house.sources = dict(sources)

    def physical_closed(self):
        return set(self.house.closed)
