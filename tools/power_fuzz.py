#!/usr/bin/env python3
"""Random simulated homes for Power Hunt: base load, one always-on plug (or none), auto or guided, and sometimes the add-on dies or
the customer says stop. Every run is checked with tests/test_power.py's power_failures (P1 water words, P2 a must-stay-on plug
touched, P3 the wrong plug named, P4 a plug left off, P5 a 'nothing here' bound below the truth).

usage: python3 tools/power_fuzz.py [runs] [seed] [out.json]          (simulation only; proves the policy, not a real meter)
"""
import os, random, sys, warnings
from collections import Counter
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
warnings.simplefilter("ignore", ResourceWarning)
from huntkit.core import ROOT
from tests.test_power import power_failures, run_power

runs = int(sys.argv[1]) if len(sys.argv) > 1 else 300
rng = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 7)
plugs = ["tv", "desk", "counter", "bedside"]
bad, outcomes, found_right, cases = [], Counter(), 0, Counter()
finished = Counter()            # runs with an always-on plug that the hunt finished: named it, or named nothing
missed = []                     # (base W, plug W) of finished runs that named nothing
for i in range(runs):
    src = {ROOT: round(rng.uniform(5, 250), 1)}
    if rng.random() < 0.8:
        src[rng.choice(plugs)] = round(rng.uniform(6, 200), 1)
    mode = rng.choice(["auto", "auto", "guided"])
    kw = {}
    r = rng.random()
    if mode == "auto" and r < 0.15:
        kw["dead_at"] = rng.randint(5, 120); cases["add-on dies"] += 1
    elif r < 0.3:
        kw["stop_at"] = rng.randint(5, 120); cases["customer says stop"] += 1
    cases[mode] += 1
    o = run_power(sources=src, mode=mode, **kw)
    f = power_failures(o)
    outcomes[o["final"].get("outcome") or o["final"].get("status")] += 1
    truth = {k for k, v in src.items() if k != ROOT}
    if truth and set(o["final"].get("found") or []) == truth:
        found_right += 1
    if truth and o["final"].get("status") == "done":
        got = set(o["final"].get("found") or [])
        finished["named it" if got == truth else "named nothing" if not got else "named another plug"] += 1
        if not got:
            missed.append((src[ROOT], max(v for k, v in src.items() if k != ROOT)))
    if f:
        bad.append((i, src, mode, kw, f))
print(f"runs={runs} failures={len(bad)} named_the_right_plug={found_right} cases={dict(cases)}")
print("outcomes:", dict(outcomes))
print("finished hunts with an always-on plug:", dict(finished))
if missed:
    r = [p / b for b, p in missed]
    print(f"  named nothing: plug draw / base load from {min(r):.2f} to {max(r):.2f}; largest plug draw missed {max(p for _, p in missed):.1f} W")
import json
out = sys.argv[3] if len(sys.argv) > 3 else None
if out:
    json.dump(dict(runs=runs, failures=len(bad), cases=dict(cases), outcomes=dict(outcomes), finished_with_plug=dict(finished),
                   missed=[dict(base_w=b, plug_w=p) for b, p in missed], fails=[str(b) for b in bad[:20]]), open(out, "w"), indent=1)
for b in bad[:10]:
    print("  FAIL", b)
sys.exit(1 if bad else 0)
