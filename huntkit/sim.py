"""Virtual house: ground-truth physics + a flow-sensor telemetry stream.

Everything the real firmware does that matters to the engine is mirrored here:
  * 1 pulse = 1/ppl litre, totals printed with 3 decimals (firmware: String(total, 3))
  * a reading every `cadence_s` seconds carrying cumulative totals since boot
  * reboot resets totals/uptime and changes boot_id
  * the safety cutoff closes the valve, sets leak=True, and flow stops

This is a *model*. Its sensor response is an assumption until the bench measures it.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .core import Config, Reading, Topology, ROOT
from .engine import HuntEngine, View


class VirtualHouse:
    def __init__(self, topo: Topology, sources: Dict[str, float], cfg: Config = Config(), *,
                 t0: float = 1_000_000.0, seed: int = 0, noise: float = 0.0,
                 dead_lpm: float = 0.0, boot_id: str = "boot-1"):
        self.topo, self.cfg = topo, cfg
        self.sources = dict(sources)               # leaf -> lpm
        self.t = t0
        self.rng = random.Random(seed)
        self.noise = noise                         # multiplicative flow jitter (sd, fraction)
        self.dead_lpm = dead_lpm                   # sensor registers nothing below this flow
        self.boot_id = boot_id
        self.boot_n = 1
        self.uptime_ms = 5_000
        self._pulses_exact = 0.0                   # exact cumulative pulses (float)
        self.closed: Set[str] = set()              # PHYSICAL state
        self.extra_lpm = 0.0                       # unmodelled interference flow
        self.valve = "OPEN"
        self.leak = False
        self._seq = 0

    # ----------------------------------------------------------------- physics
    def true_flow_lpm(self) -> float:
        if self.valve != "OPEN":
            return 0.0
        q = sum(v for leaf, v in self.sources.items() if not self.topo.blocked(leaf, self.closed))
        return q + self.extra_lpm

    def _sensed_lpm(self) -> float:
        q = self.true_flow_lpm()
        if q < self.dead_lpm:
            return 0.0
        if self.noise > 0 and q > 0:
            q *= max(0.0, 1.0 + self.rng.gauss(0.0, self.noise))
        return q

    def advance(self, dt: float, step: float = 1.0) -> None:
        """Advance physical time in 1 s increments (matches firmware's 1 Hz sampling)."""
        left = dt
        while left > 1e-9:
            s = min(step, left)
            self._pulses_exact += self._sensed_lpm() * self.cfg.ppl / 60.0 * s
            self.t += s
            self.uptime_ms += int(round(s * 1000))
            left -= s

    def sample(self) -> Reading:
        self._seq += 1
        total_l = round(int(self._pulses_exact) / self.cfg.ppl, 3)
        return Reading(sample_id=f"{self.boot_id}-{self._seq}", boot_id=self.boot_id, uptime_ms=self.uptime_ms,
                       t=self.t, out_total_l=total_l, valve=self.valve, leak=self.leak)

    # ----------------------------------------------------------------- events
    def reboot(self) -> None:
        self.boot_n += 1
        self.boot_id = f"boot-{self.boot_n}"
        self.uptime_ms = 0
        self._pulses_exact = 0.0
        self._seq = 0

    def cutoff(self) -> None:
        self.valve, self.leak = "CLOSED", True


@dataclass
class Sim:
    """Couples house + engine + cadence + a scripted user."""

    house: VirtualHouse
    engine: HuntEngine
    cadence_s: float = 10.0
    _next_sample: float = field(default=0.0)
    log: List[str] = field(default_factory=list)

    def __post_init__(self):
        self._next_sample = self.house.t + self.cadence_s

    def run(self, seconds: float, deliver: Optional[Callable[[Reading], None]] = None) -> None:
        end = self.house.t + seconds
        while self.house.t < end - 1e-9:
            step = min(1.0, end - self.house.t, self._next_sample - self.house.t)
            if step <= 1e-9:
                step = 1e-9
            self.house.advance(step, step=step)
            if self.house.t >= self._next_sample - 1e-9:
                r = self.house.sample()
                (deliver or self.engine.ingest)(r)
                self._next_sample += self.cadence_s

    def user(self, kind: str, node: Optional[str] = None, *, physical: bool = True, delay: float = 3.0) -> None:
        """The user acts, then reports `delay` seconds later (they say 'done')."""
        if physical:
            if kind == "close" and node:
                self.house.closed.add(node)
            elif kind == "open" and node:
                self.house.closed.discard(node)
        self.run(delay)
        self.engine.report(self.house.t, kind, node)

    def autopilot(self, *, max_s: float = 900.0, report_delay: float = 3.0, obey: bool = True) -> View:
        """Follow the engine's instructions like a compliant user until it finishes."""
        t_end = self.house.t + max_s
        v = self.engine.view(self.house.t)
        while self.house.t < t_end:
            v = self.engine.view(self.house.t)
            a = v.action
            if a.kind in ("done", "abort"):
                return v
            if a.kind == "wait":
                self.run(2.0)
                continue
            if a.kind == "quiet":
                self.user("quiet", None, delay=report_delay)
                continue
            if a.kind in ("close", "open"):
                node = (a.close or a.open)[0]
                self.user(a.kind, node, delay=report_delay)
                continue
            if a.kind == "set_state":
                for n in a.close:
                    self.house.closed.add(n)
                for n in a.open:
                    self.house.closed.discard(n)
                self.run(report_delay)
                for n in a.close:
                    self.engine.report(self.house.t, "close", n)
                for n in a.open:
                    self.engine.report(self.house.t, "open", n)
                continue
        return self.engine.view(self.house.t)


def demo_topology() -> Topology:
    from .core import Node
    return Topology([
        Node("bath", "bathroom branch", ROOT, prior=1, cost=1.0, how="the quarter-turn valve by the bathroom door",
             room="bathroom"),
        Node("toilet", "toilet", "bath", prior=3, cost=1.0, how="under the cistern", room="bathroom"),
        Node("shower", "shower", "bath", prior=1, cost=1.0, how="behind the access panel", room="bathroom"),
        Node("kitchen1", "kitchen pipe 1", ROOT, prior=1, cost=1.0, how="under the kitchen sink", room="kitchen",
             number=1, aliases=("sink",)),
        Node("kitchen2", "kitchen pipe 2", ROOT, prior=1, cost=1.0, how="behind the dishwasher", room="kitchen",
             number=2, aliases=("dishwasher",)),
        Node("garden", "garden tap", ROOT, prior=1, cost=1.0, how="by the back door", room="garden"),
    ])


def synth_history(t_end: float, nights: int = 14, leak_from_night: Optional[int] = None, leak_lpm: float = 2.0,
                  cfg: Config = Config(), tz_min: int = 330, cadence_s: float = 10.0,
                  boot_id: str = "hist-boot", include_today: bool = False) -> List[Reading]:
    """Closed-form overnight history (00:00-06:00 local per night), clearly synthetic.

    Night index 0 is the oldest. If `leak_from_night` is set, a continuous `leak_lpm` flow starts at
    02:10 local on that night and never stops (a toilet that started running).
    """
    out: List[Reading] = []
    day_len = 86400.0
    local_end = t_end + tz_min * 60.0
    last_midnight_local = (local_end // day_len) * day_len
    total_l = 0.0
    seq = 0
    uptime = 0
    rng = random.Random(7)
    for k in range(nights, -1 if include_today else 0, -1):
        start_local = last_midnight_local - k * day_len
        for s in range(0, int(6 * 3600), int(cadence_s)):
            t_local = start_local + s
            t = t_local - tz_min * 60.0
            if t > t_end:
                break
            flow_lpm = 0.0
            if s > 5 * 3600 + 30 * 60:                      # early-morning use after 05:30
                flow_lpm = 3.0 * (rng.random() < 0.15)
            if leak_from_night is not None and (nights - k) >= leak_from_night:
                if (nights - k) > leak_from_night or s >= 2 * 3600 + 10 * 60:
                    flow_lpm += leak_lpm
            total_l += flow_lpm / 60.0 * cadence_s
            uptime += int(cadence_s * 1000)
            seq += 1
            out.append(Reading(f"h{seq}", boot_id, uptime, t, round(int(total_l * cfg.ppl) / cfg.ppl, 3)))
    return out
