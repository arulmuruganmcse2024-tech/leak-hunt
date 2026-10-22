#!/usr/bin/env python3
"""Randomised fills ("fill the bath with N litres", "fill the pot", "water the plants") on the simulated house, checked against the
ground truth.

Each scenario draws a tap (the bath, the kitchen tap or the garden hose, each with its own rate and the branch above it), a target
(from 1 litre to that tap's maximum), water already moving in the house (none, a leak above or below the sensor floor,
two leaks), someone else's tap running before the fill, switched on or off in the 25 seconds before the request (which no window
inside the fill can see: a long fill's one-time re-check pause has to catch it), and/or switched on or off during it, and at most
one fault: the
meter's link dropping, the add-on dying, the pipe controller becoming unreachable, the tap's valve jamming open, the branch above
the tap being switched off by voice, or the customer saying stop. It then ticks the service once a simulated second and
applies the checks of tests/test_fill.py (`fill_failures`):

  F1  the tap is never open without a lease on the controller
  F2  the tap never delivers more than target + 1.5 litres
  F3  a fill that says "done" says a number within 1.5 litres of what really went in
      With an outside flow change of D litres a minute during the fill, F2 and F3 allow D times the time it can take to see it:
      at most two minutes when D is above the detection threshold of a two-minute window (`Filler.threshold_lpm`, from the
      engine's own noise model), and the rest of the fill when it is below, because then it can go unnoticed. A change in the
      seconds before the request is allowed D times the time from the change to the tap opening when the design requires a
      re-check (a fill expected to run 90 s or more that lasted past its re-check point), and D times the whole fill otherwise.
      Whether a re-check was required is worked out from the inputs, never from the fill's own record, so a fill that skips it
      (`no_recheck`) is caught. Those
      allowances are the documented limits (docs/HONEST_UNKNOWNS.md); the run reports how much of them was used.
  F4  every fill ends (unless the add-on is dead, when the controller's timer must close the tap: F8)
  F5  the tap is never extended while the meter data is stale
  F6  stop closes the tap at once
  F7  a jammed tap gets the branch above it closed
  F8  a dead add-on never leaves the tap open
  F9  a fill that is refused never opens the tap

FILL_MUTANT=<name> breaks the fill on purpose; each must be caught:
  long_lease (the lease is always 60 s, not the remaining time)  no_baseline (water already moving is counted as bath water)
  ignore_change (no change detection)  renew_stale (keeps extending on stale data)  no_backup (no branch closed for a jammed tap)
  no_stop_close (stop does not close the tap)  no_recheck (no re-check pause)

Everything is SIMULATION. It checks the add-on's policy against a model, not any real valve or meter.

usage: python3 -I tools/fill_fuzz.py [scenarios=300] [seed0=0] [out=docs/check_fill_fuzz.json]
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import warnings
from collections import Counter

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
warnings.simplefilter("ignore")

import addon.fill as fill_mod  # noqa: E402
from addon.service import DEMO_OUTLETS  # noqa: E402
from tests.test_fill import TOL_L, at, fill_failures, jam, run_fill, stop  # noqa: E402

LEAVES = ["toilet", "shower", "kitchen1", "kitchen2", "garden"]
TAPS = {o["id"]: o for o in DEMO_OUTLETS}
TAP_WEIGHTS = {"tub": 5, "ktap": 3, "hose": 2}


def draw(seed: int) -> dict:
    r = random.Random(seed)
    tap = r.choices(list(TAP_WEIGHTS), list(TAP_WEIGHTS.values()))[0]
    mx = TAPS[tap]["max_l"]
    target = round(r.choice([r.uniform(1, min(20, mx)), r.uniform(min(20, mx) / 2, mx), r.uniform(0.6 * mx, mx)]), 1)
    n = r.choices([0, 1, 2], [3, 5, 2])[0]
    leaks = {l: round(r.uniform(1.4, 4.0), 2) for l in r.sample(LEAVES, n)}
    if r.random() < 0.1:
        leaks = {r.choice(LEAVES): 0.4}
    pre = round(r.uniform(1.2, 3.0), 2) if r.random() < 0.3 else 0.0
    pre_change = (dict(before_s=round(r.uniform(1, 24), 1), lpm=r.choice([0.0, round(r.uniform(1.2, 3.0), 2)]))
                  if r.random() < 0.25 else None)
    dur = target / TAPS[tap]["sim_lpm"] * 60.0                 # roughly how long this fill runs, so events land inside it
    changes = []
    for _ in range(r.choices([0, 1, 2], [6, 3, 1])[0]):
        changes.append(dict(t=round(r.uniform(15, max(60.0, 1.3 * dur)), 1), lpm=r.choice([0.0, round(r.uniform(1.2, 3.0), 2)])))
    fault = r.choices([None, "blackout", "dead", "offline", "jam", "branch_off", "stop"], [10, 2, 2, 2, 1, 1, 2])[0]
    return dict(seed=seed, tap=tap, target=target, leaks=leaks, pre_ambient=pre, pre_change=pre_change, changes=sorted(changes, key=lambda c: c["t"]), fault=fault,
                fault_t=round(r.uniform(5, max(30.0, 1.1 * dur)), 1), fault_dur=r.choice([20, 45, 90, 200]))


def run(sc: dict) -> dict:
    events = [at(c["t"], (lambda lpm: lambda svc, src, hub, clock, out: src.set_ambient(lpm, clock()))(c["lpm"])) for c in sc["changes"]]
    f, t, d = sc["fault"], sc["fault_t"], sc["fault_dur"]
    dead = None
    if f == "blackout":
        events += [at(t, lambda svc, src, hub, clock, out: setattr(src, "blackout", True)),
                   at(t + d, lambda svc, src, hub, clock, out: setattr(src, "blackout", False))]
    elif f == "dead":
        dead = t
    elif f == "offline":
        events += [at(t, lambda svc, src, hub, clock, out: setattr(hub, "offline", True)),
                   at(t + d, lambda svc, src, hub, clock, out: setattr(hub, "offline", False))]
    elif f == "jam":
        events.append(at(t, jam))
    elif f == "branch_off":
        events.append(at(t, lambda svc, src, hub, clock, out: _quiet(svc.set_pipe, "h", svc._topology("h").label(out["parent"]), "off")))
    elif f == "stop":
        events.append(at(t, lambda svc, src, hub, clock, out: _quiet(stop, svc, src, hub, clock, out)))
    pre = sc["pre_ambient"]
    pc = sc.get("pre_change")
    when = {}
    pre_events = [(pc["before_s"], lambda svc, src, hub, clock: when.__setitem__("t", clock()) or src.set_ambient(pc["lpm"], clock()))] if pc else ()
    o = run_fill(target=sc["target"], sources=dict(sc["leaks"]), events=events, dead_at=dead, outlet=TAPS[sc["tap"]]["label"],
                 before=(lambda svc, src, hub, clock: src.set_ambient(pre, clock())) if pre else None, pre_events=pre_events)
    if o.get("refused") is not None:
        return dict(seed=sc["seed"], tap=sc["tap"], outcome="refused", truth=round(o["truth"], 2), said=0.0, target=sc["target"],
                    overfill=round(o["truth"] - sc["target"], 2), changed=False, allowance_l=0.0, fault=sc["fault"],
                    violations=fill_failures(o), refused=o["refused"])
    fails = fill_failures(o)
    fin = o["final"]
    t_end = (fin["ended"] if fin["ended"] is not None else o["clock"]()) - fin["started"]
    changed = [c for c in sc["changes"] if c["t"] < t_end]
    allow = 0.0
    level = pre
    pre_changed = bool(pc) and abs(pc["lpm"] - pre) > 0.01
    if pre_changed:                               # a change before the request: the re-check corrects it, or nothing can see it
        dpre = abs(pc["lpm"] - pre)
        expected = sc["target"] / TAPS[sc["tap"]]["nominal_lpm"] * 60.0          # whether the design says it must re-check,
        due = max(fill_mod.RECHECK_AFTER_S, fill_mod.RECHECK_AT * expected)         # from the inputs, not from the fill's own record
        must = expected >= fill_mod.RECHECK_MIN_FILL_S and t_end >= due + 45.0
        allow += dpre * ((o["t_request"] - when["t"] + 2.0) if must else (t_end + 25.0)) / 60.0
        level = pc["lpm"]
    base = sum(sc["leaks"].values()) + TAPS[sc["tap"]]["sim_lpm"]
    for c in changed:                             # what an outside change can cost before it is seen (or for the rest of the fill)
        delta = abs(c["lpm"] - level)
        thr = o["svc"].filler.threshold_lpm(base + max(level, c["lpm"]), 12)
        allow += delta * (min(120.0, t_end - c["t"]) if delta >= thr else (t_end - c["t"])) / 60.0
        level = c["lpm"]
    if changed or pre_changed:
        fails = [x for x in fails if not (x.startswith("F2") or x.startswith("F3"))]
        if o["truth"] > sc["target"] + TOL_L + allow:
            fails.append(f"F2 over-filled with an outside change: {o['truth']:.1f} litres for {sc['target']:.0f} (allowed {TOL_L + allow:.1f} over)")
        if fin["outcome"] == "done" and abs(fin["state"]["delivered_l"] - o["truth"]) > TOL_L + allow:
            fails.append(f"F3 said {fin['state']['delivered_l']:.1f} litres, really {o['truth']:.1f} (allowed {TOL_L + allow:.1f})")
    return dict(seed=sc["seed"], tap=sc["tap"], outcome=fin["outcome"] or ("open_dead" if fin["ended"] is None else ""), truth=round(o["truth"], 2),
                said=round(fin["state"]["delivered_l"], 2), target=sc["target"], overfill=round(o["truth"] - sc["target"], 2),
                changed=bool(changed), pre_changed=pre_changed, rechecked=bool(fin["state"].get("rechecked")),
                allowance_l=round(allow, 2), fault=sc["fault"], violations=fails)


def _quiet(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    seed0 = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(ROOT, "docs", "check_fill_fuzz.json")
    t0 = time.time()
    rows = [run(draw(seed0 + i)) for i in range(n)]
    codes = Counter(v.split()[0] for r in rows for v in r["violations"])
    quiet = lambda r: not r["changed"] and not r.get("pre_changed")              # no outside change before or during the fill
    clean = [r for r in rows if quiet(r) and r["fault"] in (None,)]
    done = [r for r in rows if r["outcome"] == "done"]
    summary = dict(scenarios=n, seed0=seed0, mutant=fill_mod.MUTANT, seconds=round(time.time() - t0), violations=sum(codes.values()),
                   by_check=dict(codes), outcomes=dict(Counter(r["outcome"] for r in rows)),
                   taps=dict(Counter(r["tap"] for r in rows)),
                   outcomes_by_tap={t: dict(Counter(r["outcome"] for r in rows if r["tap"] == t)) for t in TAP_WEIGHTS},
                   max_overfill_l_by_tap={t: max([r["overfill"] for r in rows if r["tap"] == t] or [0]) for t in TAP_WEIGHTS},
                   max_overfill_l=max(r["overfill"] for r in rows), max_overfill_l_clean=max([r["overfill"] for r in clean] or [0]),
                   max_overfill_l_no_outside_change=max([r["overfill"] for r in rows if quiet(r)] or [0]),
                   max_overfill_l_with_outside_change=max([r["overfill"] for r in rows if not quiet(r)] or [0]),
                   max_said_vs_truth_l_when_done=max([abs(r["said"] - r["truth"]) for r in done] or [0]),
                   max_said_vs_truth_l_when_done_clean=max([abs(r["said"] - r["truth"]) for r in done if quiet(r)] or [0]),
                   scenarios_with_an_outside_change=sum(1 for r in rows if r["changed"]),
                   scenarios_with_a_change_before_the_request=sum(1 for r in rows if r.get("pre_changed")),
                   max_overfill_l_change_before_request_rechecked=max([r["overfill"] for r in rows if r.get("pre_changed") and r.get("rechecked") and not r["changed"]] or [0]),
                   max_overfill_l_change_before_request_not_rechecked=max([r["overfill"] for r in rows if r.get("pre_changed") and not r.get("rechecked") and not r["changed"]] or [0]),
                   fills_that_rechecked=sum(1 for r in rows if r.get("rechecked")),
                   first_failures=[r for r in rows if r["violations"]][:5])
    with open(out, "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1)[:3000])
    sys.exit(1 if summary["violations"] and not fill_mod.MUTANT else 0)


if __name__ == "__main__":
    if os.environ.get("FILL_MUTANT"):
        fill_mod.MUTANT = os.environ["FILL_MUTANT"]
    main()
