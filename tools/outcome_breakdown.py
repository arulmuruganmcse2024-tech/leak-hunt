#!/usr/bin/env python3
"""What did the automatic hunt actually DO across the fuzzer's 300 scenarios: right valve, nothing named, or a wrong valve?

"No wrong valve" alone could be achieved by a hunt that never names anything. This reads the same 300 scenarios the fuzzer runs
(seeds 0..299, `tools/auto_fuzz.py draw/run`) and sorts every one by what was really hidden and what the hunt ended up saying,
so the number of correct answers sits next to the number of refusals and misses. It judges nothing new: it re-reads the runs.

    python3 -I tools/outcome_breakdown.py            # about 3-4 minutes -> docs/check_outcomes.json

Simulation only: fake clock, simulated house, simulated pipe controller, and a simulated customer who is quick, slow or never
answers. The shares below describe THIS scenario mix, not any real home. Seconds are simulated seconds.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import warnings
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
N = 300
FLOOR_LPM = 1.0                     # the engine's assumed sensor floor (huntkit Config.floor_lpm); an assumption, not a measurement

# the buckets, in the order they are printed
FINDABLE = ("named_a_valve_with_a_leak", "named_a_dry_valve", "stopped_by_the_house_or_a_fault", "customer_never_answered",
            "said_where_the_water_is_not", "other_nothing_named")
NOTHING = ("said_nothing_above_the_limit", "named_a_valve", "customer_never_answered", "stopped_by_the_house_or_a_fault", "other")


def classify(sc: dict, r: dict, floor: float = FLOOR_LPM) -> tuple:
    """(group, bucket) for one finished scenario. group is 'findable' (a leak above the floor was hidden) or 'nothing_to_find'."""
    findable = any(q >= floor for q in sc["leaks"].values())
    outcome = str(r["outcome"])
    if findable:
        if r["found"]:
            return "findable", "named_a_dry_valve" if r["false_accusation"] else "named_a_valve_with_a_leak"
        if sc["human"] == "absent" and outcome == "abandoned":
            return "findable", "customer_never_answered"
        if outcome in ("upstream_remainder", "no_flow_above_limit", "below_sensor_floor"):
            return "findable", "said_where_the_water_is_not"
        if outcome.startswith("aborted") or outcome == "stopped":
            return "findable", "stopped_by_the_house_or_a_fault"
        return "findable", "other_nothing_named"
    if r["found"]:
        return "nothing_to_find", "named_a_valve"
    if outcome in ("no_flow_above_limit", "below_sensor_floor"):
        return "nothing_to_find", "said_nothing_above_the_limit"
    if sc["human"] == "absent" and outcome == "abandoned":
        return "nothing_to_find", "customer_never_answered"
    if outcome.startswith("aborted") or outcome == "stopped":
        return "nothing_to_find", "stopped_by_the_house_or_a_fault"
    return "nothing_to_find", "other"


def summarise(rows: list) -> dict:
    """rows: [{sc, r}] for the scenarios. Counts per bound (in the proven bound or not), group and bucket."""
    out = {"in_bound": {}, "outside_bound": {}}
    for key, pick in (("in_bound", True), ("outside_bound", False)):
        sel = [x for x in rows if x["r"]["in_bound"] is pick]
        groups = {}
        for g in ("findable", "nothing_to_find"):
            part = [x for x in sel if classify(x["sc"], x["r"])[0] == g]
            buckets = Counter(classify(x["sc"], x["r"])[1] for x in part)
            groups[g] = dict(total=len(part), buckets={b: buckets.get(b, 0) for b in (FINDABLE if g == "findable" else NOTHING)})
        out[key] = dict(total=len(sel), **groups)
    return out


def main() -> None:
    sys.path.insert(0, ROOT)
    warnings.simplefilter("ignore")
    from tools import auto_fuzz as af
    t0 = time.time()
    rows = []
    for i in range(N):
        sc = af.draw(i)
        rows.append(dict(sc=sc, r=af.run(sc)))
        if (i + 1) % 50 == 0:
            print(f"{i + 1}/{N} {time.time() - t0:.0f}s", flush=True)
    s = summarise(rows)
    ib = [x for x in rows if x["r"]["in_bound"]]
    found_times = [x["r"]["virtual_s"] for x in ib if classify(x["sc"], x["r"]) == ("findable", "named_a_valve_with_a_leak")]
    present = [x for x in ib if classify(x["sc"], x["r"])[0] == "findable" and x["sc"]["human"] != "absent"]
    res = dict(scenarios=N, seed0=0, floor_lpm_assumed=FLOOR_LPM, **s,
               findable_with_a_present_customer=dict(
                   total=len(present),
                   named_a_valve_with_a_leak=sum(1 for x in present if classify(x["sc"], x["r"])[1] == "named_a_valve_with_a_leak"),
                   named_a_dry_valve=sum(1 for x in present if classify(x["sc"], x["r"])[1] == "named_a_dry_valve")),
               wrong_valve_outside_the_bound=sum(1 for x in rows if not x["r"]["in_bound"] and x["r"]["false_accusation"]),
               simulated_seconds_to_a_named_valve=dict(median=statistics.median(found_times), max=max(found_times), n=len(found_times)),
               seconds=round(time.time() - t0))
    with open(os.path.join(ROOT, "docs", "check_outcomes.json"), "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res, indent=1))
    fz = os.path.join(ROOT, "docs", "check_auto_fuzz.json")
    if os.path.exists(fz):                                       # the same scenarios as the fuzzer's own summary, or something is off
        z = json.load(open(fz))
        same = (z["in_bound_scenarios"] == res["in_bound"]["total"] and z["in_bound_with_a_finding"] ==
                sum(1 for x in ib if x["r"]["found"]))
        print("matches docs/check_auto_fuzz.json:", same)
        if not same:
            raise SystemExit("the scenarios here are not the fuzzer's scenarios")


if __name__ == "__main__":
    main()
