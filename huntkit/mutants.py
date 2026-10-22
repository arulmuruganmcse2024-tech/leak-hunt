"""Deliberately broken engines. The checker must catch every one of them."""
from __future__ import annotations

from dataclasses import replace

from .engine import CONFIRMED, TENTATIVE, HuntEngine


class SingleDropConfirms(HuntEngine):
    """M1: skips replication - a single drop is called a finding."""

    def _verdicts(self, ms):
        verdicts, info, cons = super()._verdicts(ms)
        for v, s in list(verdicts.items()):
            if s == TENTATIVE:
                verdicts[v] = CONFIRMED
        return verdicts, info, cons


class IgnoreValveInWindows(HuntEngine):
    """M2: windows may contain CLOSED/leak readings (safety valve closing looks like 'found it')."""

    def _measure_epoch(self, i, eps, readings, now):
        clean = [replace(r, valve="OPEN", leak=False) for r in readings]
        return super()._measure_epoch(i, eps, clean, now)


class SameSideReplication(HuntEngine):
    """M3: accepts two drops that are not on opposite sides of the middle epoch (not a real A-B-A)."""

    def _verdicts(self, ms):
        # re-implement the triple search without the ordering rule
        cons = self._contrasts(ms)
        verdicts, info, cons0 = super()._verdicts(ms)
        for v, cs in cons.items():
            drops = [c for c in cs if c.kind == "drop"]
            if len(drops) >= 2 and verdicts[v] != CONFIRMED and self._consistent(drops[0], drops[1]):
                verdicts[v] = CONFIRMED
                info[v] = dict(delta=drops[0].delta_lpm, sd=drops[0].sd_lpm, evidence=[drops[0].o, drops[0].x])
        return verdicts, info, cons0


class NoRebootGuard(HuntEngine):
    """M4: treats readings of different boots as one stream (cross-boot differencing)."""

    def _measure_epoch(self, i, eps, readings, now):
        glued = [replace(r, boot_id="same") for r in readings]
        return super()._measure_epoch(i, eps, glued, now)


class NoRebootNoCounterChecks(HuntEngine):
    """M5: M4 plus a device whose uptime looks continuous and whose counter never visibly drops.

    The engine's own counter and uptime checks are what shield M4. Removing that second layer
    leaves nothing between a reboot and a false reading.
    """

    def _measure_epoch(self, i, eps, readings, now):
        best = 0.0
        glued = []
        for r in readings:
            best = max(best, r.out_total_l)
            glued.append(replace(r, boot_id="same", uptime_ms=int(r.t * 1000), out_total_l=best))
        return super()._measure_epoch(i, eps, glued, now)


MUTANTS = {
    "M1_single_drop_confirms": SingleDropConfirms,
    "M2_ignore_valve_in_windows": IgnoreValveInWindows,
    "M3_same_side_replication": SameSideReplication,
    "M4_no_reboot_guard": NoRebootGuard,
    "M5_no_reboot_no_counter_checks": NoRebootNoCounterChecks,
}
