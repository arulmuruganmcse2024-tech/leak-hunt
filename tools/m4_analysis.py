"""What does the reboot guard add on top of the counter, uptime and clock checks?  (mutant M4)

M4 (`huntkit.mutants.NoRebootGuard`) glues every reading to one boot, so a measurement window may span a reboot. The
engine still refuses a window when
  * a counter goes down, or an uptime does not increase, between neighbouring readings, or
  * the wall-clock span of the window disagrees with the uptime span by more than `clock_tol_s`.

Derivation (a = first reading of the window, in boot 1; b = last reading, in boot 2; boot k starts at B_k; u = uptime):
    t_b - t_a = (B_2 + u_b) - (B_1 + u_a)         and          T = u_b - u_a
    clock check:  | (t_b - t_a) - T | <= tol    <=>    | B_2 - B_1 | <= tol
Boot 2 starts after a was taken, so B_2 >= t_a = B_1 + u_a, hence  u_a <= tol: the window can only begin inside the first
`tol` seconds of boot 1 and the second boot must follow almost immediately - a boot loop. The uptime check adds that the
first sample of boot 2 must be later in its boot than a was in boot 1. For a constant flow q the counters are q*u (litres
since boot), so  n/T = q*(u_b - u_a)/(u_b - u_a) = q: where M4 does accept such a window, the rate it measures is right.

This script checks that against the real engine code on a grid of schedules (not by trust in the algebra): every
schedule is run through the real engine and through M4. The windows M4 resolves across a reboot are counted, with the age
of boot 1 at the window's first reading, the error of the measured rate under constant flow, and what the real engine does
with the same readings.

    python3 -I tools/m4_analysis.py            # writes docs/check_m4_equivalence.json
"""
from __future__ import annotations

import itertools
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from huntkit.core import Config, Node, Reading, Topology  # noqa: E402
from huntkit.engine import HuntEngine, Report  # noqa: E402
from huntkit.mutants import NoRebootGuard  # noqa: E402


def build(B1, B2, boot1, boot2, q1, q2, jitter, horizon):
    """Readings of a device that reboots once, at B2. Each boot: a first sample at uptime `first_e` (optional) and then
    samples at uptime `phase + 10 n`. Counter = litres since boot = q * uptime / 60 (constant flow per boot)."""
    out = []
    for k, (B, (phase, first_e), q, end, jit) in enumerate(((B1, boot1, q1, B2, 0.0), (B2, boot2, q2, horizon, jitter))):
        ups = ([first_e] if first_e is not None else []) + [phase + 10.0 * n for n in range(0, 40)]
        for u in ups:
            if B + u >= end:
                break
            out.append(Reading(sample_id=f"b{k}-{u:.2f}", boot_id=f"boot{k}", uptime_ms=int(round(u * 1000)),
                               t=B + u + jit, out_total_l=q * u / 60.0))
    return sorted(out, key=lambda r: r.t)


def main() -> None:
    cfg = Config()
    topo = Topology([Node("a", "a")])
    real, m4 = HuntEngine(topo, cfg), NoRebootGuard(topo, cfg)
    t0 = 1000.0
    eps = [(t0, frozenset(), Report(0, t0, "quiet"))]
    stats = Counter()
    examples = []
    worst = dict(age_a=0.0, err_const=0.0)
    boots1 = list(itertools.product((0.5, 2.5), (None, 0.5, 2.0)))
    boots2 = list(itertools.product((0.5, 2.5, 5.0, 10.0), (None, 0.5, 1.0, 2.0, 2.9, 4.0)))
    flows = ((0, 0), (2, 2), (3, 3), (2, 0), (0, 2), (3, 1))
    for horizon_s in range(14, 70, 5):                                                # how long after the step we look
        now = t0 + horizon_s
        for A1 in (0.5, 1, 2, 2.9, 3, 3.5, 4, 8, 20):                                 # age of boot 1 when it is replaced
            for off in range(6, 60, 2):                                                # the reboot happens `off` s after t0
                B2 = t0 + off
                B1 = B2 - A1
                if B2 > now:
                    continue
                for boot1, boot2, (q1, q2), jit in itertools.product(boots1, boots2, flows, (0.0, 2.0, -2.0)):
                    rs = build(B1, B2, boot1, boot2, q1, q2, jit, now)
                    if len(rs) < 2:
                        continue
                    a = real._measure_epoch(0, eps, rs, now)
                    b = m4._measure_epoch(0, eps, rs, now)
                    stats["schedules"] += 1
                    if b.status != "resolved":
                        continue
                    ra = next(r for r in rs if r.sample_id == b.a_id)
                    rb = next(r for r in rs if r.sample_id == b.b_id)
                    if ra.boot_id == rb.boot_id:
                        continue
                    stats["M4 resolved a window that spans a reboot"] += 1
                    age_a = ra.uptime_ms / 1000.0
                    worst["age_a"] = max(worst["age_a"], age_a)
                    if q1 == q2:
                        worst["err_const"] = max(worst["err_const"], abs(b.lpm - q1))
                    if a.status == "resolved" and abs(a.lpm - b.lpm) < 1e-9:
                        stats["  the real engine resolves the same rate"] += 1
                    elif a.status == "resolved":
                        stats["  the real engine resolves a different rate (flow changed at the reboot)" if q1 != q2
                              else "  the real engine resolves a different rate with CONSTANT flow"] += 1
                    else:
                        stats["  the real engine does not resolve yet (" + a.status + ": " + a.reason + ")"] += 1
                        if q1 != q2:
                            stats["    ...of which with a flow change at the reboot"] += 1
                        if len(examples) < 6:
                            examples.append(dict(horizon_s=horizon_s, A1=A1, off=off, boot1=boot1, boot2=boot2, q=[q1, q2],
                                                 jitter=jit, age_a_s=age_a, lpm_m4=round(b.lpm, 4), T_m4=round(b.T, 2),
                                                 real=a.status + ":" + a.reason))
    res = dict(config=dict(clock_tol_s=cfg.clock_tol_s, min_window_s=cfg.min_window_s), stats=dict(stats),
               max_age_of_boot1_at_window_start_s=worst["age_a"], max_rate_error_const_flow_lpm=worst["err_const"],
               examples=examples)
    print(json.dumps(res, indent=1))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "check_m4_equivalence.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
