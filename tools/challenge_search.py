#!/usr/bin/env python3
"""Search for the unlucky moment: one outside flow change, every timing, many hidden rounds.

A tap is already running when the hunt starts and is switched off ONCE, `t` seconds into the hunt, for t = 20, 23, ..., 197.
That is one outside flow change, the case the project claims it can never be fooled by. For each hidden round the same
readings are judged by Leak Hunt and by a deliberately naive engine that believes the first drop it sees (mutant M1, see
huntkit/mutants.py). The result says how often each of them named a valve with no leak behind it.

    python3 -I tools/challenge_search.py [seeds ...]     # default: 3 5 7 8 9 10 11 12 13 14   (about 10 minutes)
    -> docs/check_challenge_search.json
    CHALLENGE_JITTER=1 python3 -I tools/challenge_search.py [seeds ...]
    -> docs/check_challenge_search_jitter.json: the same hunts on a timeline with sub-second phases (the hunt starts a random
       fraction of a second off the sensor's sampling grid, ticks last 0.8-1.3 s, the customer answers at a random delay). The
       claim is about Leak Hunt, so this is the run that matters for it; how often the naive engine is fooled is incidental.

Simulation only: fake clock, simulated house, simulated pipe controller.
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from collections import Counter

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
warnings.simplefilter("ignore")

from addon.challenge import Challenge  # noqa: E402
from addon.service import HuntService  # noqa: E402
from addon.store import Store  # noqa: E402
from huntkit import Config  # noqa: E402

T0 = 1_800_000_000.0 + 23 * 3600
TIMINGS = list(range(20, 200, 3))


class Clock:
    def __init__(self): self.t = T0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


JITTER = os.environ.get("CHALLENGE_JITTER") == "1"


def play(seed: int, t_off: int, max_s: int = 1000) -> dict:
    import random
    jr = random.Random(seed * 1_000_003 + t_off)

    def dt() -> float:
        return jr.uniform(0.8, 1.3) if JITTER else 1.0

    clock = Clock()
    svc = HuntService(Store(), clock, Config(), key=b"search", public_base_url="https://x.test")
    svc.ensure_demo_household("h", control=True)
    ch = Challenge(svc, "h")
    ch.new_round(seed)
    ch.act("ambient_on")
    clock.advance(1 + (jr.uniform(0.05, 0.95) if JITTER else 0.0))
    hid = svc.start_hunt("h", mode="auto", consent=True, resume=False)["hunt_id"]
    t0, done = clock(), False
    for _ in range(max_s):
        clock.advance(dt())
        if not done and clock() - t0 >= t_off:
            ch.act("ambient_off"); done = True
        svc.tick()
        v = svc.hunt_status("h", hid)
        if v["status"] != "active":
            break
        if v["auto"] and v["auto"]["needs_you"] and v["action"]["kind"] == "quiet":
            clock.advance(3 + (jr.uniform(0.0, 4.0) if JITTER else 0.0)); svc.report("h", hid, "quiet")
    r = ch.reveal()
    return dict(seed=seed, t_off=t_off, truth=[t["label"] for t in r["truth"]], running=r["running"], outcome=r["hunt_outcome"],
                leak_hunt=r["engine"]["found"], leak_hunt_grade=r["engine"]["grade"],
                naive=r["naive"]["found"], naive_grade=r["naive"]["grade"], outside_changes=r["outside_changes"])


def main() -> None:
    seeds = [int(a) for a in sys.argv[1:]] or [3, 5, 7, 8, 9, 10, 11, 12, 13, 14]
    t = time.time()
    rows = []
    for seed in seeds:
        for t_off in TIMINGS:
            rows.append(play(seed, t_off))
        print(f"seed {seed} done", flush=True)
    total = len(rows)
    lh_false = [r for r in rows if r["leak_hunt_grade"] == "false_accusation"]
    nv_false = [r for r in rows if r["naive_grade"] == "false_accusation"]
    res = dict(seeds=seeds, timings_s=[TIMINGS[0], TIMINGS[-1], TIMINGS[1] - TIMINGS[0]], hunts=total,
               outside_changes_per_hunt=sorted({r["outside_changes"] for r in rows}),
               leak_hunt_false_accusations=len(lh_false), naive_false_accusations=len(nv_false),
               naive_false_accusation_cases=[dict(seed=r["seed"], t_off=r["t_off"], hidden=r["truth"], naive=r["naive"], leak_hunt=r["leak_hunt"])
                                             for r in nv_false],
               leak_hunt_grades=dict(Counter(r["leak_hunt_grade"] for r in rows)), naive_grades=dict(Counter(r["naive_grade"] for r in rows)),
               still_running_at_the_end=sum(1 for r in rows if r["running"]), seconds=round(time.time() - t))
    print(json.dumps(res, indent=1))
    res["timeline"] = "sub-second phases" if JITTER else "whole seconds"
    with open(os.path.join(ROOT, "docs", "check_challenge_search_jitter.json" if JITTER else "check_challenge_search.json"), "w") as f:
        json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
