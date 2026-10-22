#!/usr/bin/env python3
"""What did the stalled-hunt fix change? The same 300 automatic-hunt scenarios, with and without it.

`AUTO_FUZZ_MUTANT=no_remeasure` is the service as it was before the fix (an unclean measurement is waited on instead of being
measured again). This runs the fuzzer's 300 scenarios (seeds 0..299) on both and lists every scenario whose outcome, named valves
or violations differ. It judges nothing: it is the list the write-up in docs/VERIFICATION.md is checked against.

    python3 -I tools/remeasure_diff.py            # about 4 minutes (two processes) -> docs/check_remeasure_diff.json

Simulation only: fake clock, simulated house, simulated pipe controller.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import warnings
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
N = 300


def child(mutant: str) -> None:
    sys.path.insert(0, ROOT)
    warnings.simplefilter("ignore")
    from tools import auto_fuzz as af
    rows = []
    for i in range(N):
        sc = af.draw(i)
        r = af.run(sc)
        rows.append(dict(seed=sc["seed"], outcome=r["outcome"], found=r["found"], in_bound=r["in_bound"], virtual_s=r["virtual_s"],
                         violations=r["violations"]))
    json.dump(rows, sys.stdout)


def main() -> None:
    t0 = time.time()
    procs = {}
    for name, mutant in (("fixed", ""), ("before_fix", "no_remeasure")):
        env = dict(os.environ, AUTO_FUZZ_MUTANT=mutant)
        procs[name] = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--child"], env=env, stdout=subprocess.PIPE)
    out = {}
    for name, p in procs.items():
        data, _ = p.communicate()
        if p.returncode != 0:
            raise SystemExit(f"{name} run failed")
        out[name] = json.loads(data)
    a, b = out["fixed"], out["before_fix"]
    changed = []
    for x, y in zip(a, b):
        assert x["seed"] == y["seed"]
        if (x["outcome"], x["found"], bool(x["violations"])) != (y["outcome"], y["found"], bool(y["violations"])):
            changed.append(dict(seed=x["seed"], in_bound=x["in_bound"],
                                before_fix=dict(outcome=y["outcome"], found=y["found"], virtual_s=y["virtual_s"], violations=[v.split()[0] for v in y["violations"]]),
                                fixed=dict(outcome=x["outcome"], found=x["found"], virtual_s=x["virtual_s"], violations=[v.split()[0] for v in x["violations"]])))
    res = dict(scenarios=N, changed=len(changed),
               in_bound_with_a_finding=dict(fixed=sum(1 for x in a if x["in_bound"] and x["found"]), before_fix=sum(1 for x in b if x["in_bound"] and x["found"])),
               violations=dict(fixed=sum(len(x["violations"]) for x in a), before_fix=sum(len(x["violations"]) for x in b)),
               outcomes=dict(fixed=dict(Counter(x["outcome"].split(":")[0] for x in a)), before_fix=dict(Counter(x["outcome"].split(":")[0] for x in b))),
               changed_scenarios=changed, seconds=round(time.time() - t0))
    with open(os.path.join(ROOT, "docs", "check_remeasure_diff.json"), "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "changed_scenarios"}, indent=1))
    for c in changed:
        print(c)


if __name__ == "__main__":
    if "--child" in sys.argv:
        child(os.environ.get("AUTO_FUZZ_MUTANT", ""))
    else:
        main()
