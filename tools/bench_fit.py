#!/usr/bin/env python3
"""Turn one bench session with a measuring jug into candidate values for the sensor assumptions.

    python3 -I tools/bench_fit.py session.csv [--tol 0.20] [--max-rsd 0.15] [--min-runs 3] [--ppl 360] [--json out.json]

The CSV has one row per run:  test,actual_l,reported_l,seconds
    calibration  a known volume through the sensor at a steady, moderate flow
    flow         a steady flow held for `seconds` (volume from the jug); repeat each flow level at least 3 times
    zero         no flow at all for `seconds` (actual_l is 0)
`reported_l` is how much the device's cumulative outlet volume rose during the run. Lines starting with # are ignored.

This prints CANDIDATES. The criteria (--tol, --max-rsd, --min-runs) are mine, written down so they can be argued with; nothing
here changes the engine. Take the numbers to docs/BENCH.md and decide. It is plain arithmetic on your own measurements, no
simulation, and says nothing about a house.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from typing import Dict, List


def _num(row: dict, key: str) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"bad or missing {key!r} in row {row}")


def load(path: str) -> List[dict]:
    rows = []
    with open(path, newline="") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    for r in csv.DictReader(lines):
        test = (r.get("test") or "").strip().lower()
        if test not in ("calibration", "flow", "zero"):
            raise ValueError(f"unknown test {test!r}: use calibration, flow or zero")
        row = dict(test=test, actual_l=_num(r, "actual_l"), reported_l=_num(r, "reported_l"), seconds=_num(r, "seconds"))
        if row["seconds"] <= 0:
            raise ValueError("seconds must be positive")
        if test != "zero" and row["actual_l"] <= 0:
            raise ValueError("actual_l must be positive for calibration and flow runs")
        rows.append(row)
    return rows


def _rsd(xs: List[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    m = statistics.fmean(xs)
    return statistics.stdev(xs) / m if m else float("nan")


def fit(rows: List[dict], *, tol: float = 0.20, max_rsd: float = 0.15, min_runs: int = 3, ppl: float = 360.0) -> dict:
    out: dict = {"criteria": dict(tol=tol, max_rsd=max_rsd, min_runs=min_runs, ppl_assumed=ppl)}

    cal = [r["reported_l"] / r["actual_l"] for r in rows if r["test"] == "calibration"]
    if cal:
        m = statistics.fmean(cal)
        out["calibration"] = dict(runs=len(cal), mean_ratio=round(m, 4), rel_sd=round(_rsd(cal), 4) if len(cal) > 1 else None,
                                  implied_ppl_if_device_uses_assumed=round(ppl * m, 1))

    levels: Dict[float, List[dict]] = defaultdict(list)
    for r in rows:
        if r["test"] == "flow":
            levels[round(r["actual_l"] / r["seconds"] * 60.0, 1)].append(r)
    table = []
    for lpm in sorted(levels):
        rs = levels[lpm]
        ratios = [x["reported_l"] / x["actual_l"] for x in rs]
        m = statistics.fmean(ratios)
        rsd = _rsd(ratios)
        ok = (len(rs) >= min_runs and all(abs(x - 1.0) <= tol for x in ratios)
              and (math.isnan(rsd) or rsd <= max_rsd))
        table.append(dict(true_lpm=lpm, runs=len(rs), mean_ratio=round(m, 4),
                          rel_sd=None if math.isnan(rsd) else round(rsd, 4), passes=bool(ok)))
    out["flow_levels"] = table

    # the floor is the lowest level such that it AND every level above it passes
    floor = None
    for row in reversed(table):
        if row["passes"]:
            floor = row["true_lpm"]
        else:
            break
    good = [x for x in table if floor is not None and x["true_lpm"] >= floor and x["rel_sd"] is not None]
    out["candidates"] = dict(
        floor_lpm=floor,
        rel_sd=round(max((x["rel_sd"] for x in good), default=float("nan")), 3) if good else None,
        note=("no flow level passed the criteria: the floor is above the highest level tested, or too few runs per level"
              if floor is None else "lowest tested flow at which it and every higher tested flow pass"))

    zero = [r for r in rows if r["test"] == "zero"]
    if zero:
        secs = sum(r["seconds"] for r in zero)
        litres = sum(r["reported_l"] for r in zero)
        out["zero_flow"] = dict(runs=len(zero), seconds=secs, reported_l=round(litres, 4),
                                spurious_pulses_per_s=round(litres * ppl / secs, 4),
                                note="compare with Config.noise_pps (0.02 assumed)")
    return out


def render(res: dict) -> str:
    c = res["criteria"]
    lines = [f"criteria: within +-{c['tol']:.0%} of the jug, spread <= {c['max_rsd']:.0%}, >= {c['min_runs']} runs per level",
             f"(pulses per litre assumed for conversions: {c['ppl_assumed']:g})", ""]
    if "calibration" in res:
        k = res["calibration"]
        lines.append(f"calibration: {k['runs']} runs, device/jug = {k['mean_ratio']}, spread {k['rel_sd']}; "
                     f"implied pulses per litre {k['implied_ppl_if_device_uses_assumed']} if the device uses {c['ppl_assumed']:g}")
    lines.append("flow levels (true L/min, runs, device/jug, spread, passes):")
    for x in res["flow_levels"]:
        lines.append(f"  {x['true_lpm']:>5}  {x['runs']:>2}  {x['mean_ratio']:>7}  {x['rel_sd']}  {'yes' if x['passes'] else 'NO'}")
    if "zero_flow" in res:
        z = res["zero_flow"]
        lines.append(f"zero flow: {z['reported_l']} L reported in {z['seconds']:g} s = {z['spurious_pulses_per_s']} pulses/s ({z['note']})")
    k = res["candidates"]
    lines += ["", f"candidate floor_lpm: {k['floor_lpm']}   candidate rel_sd: {k['rel_sd']}   ({k['note']})",
              "Nothing was changed. Put the numbers in docs/BENCH.md, then decide."]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv")
    ap.add_argument("--tol", type=float, default=0.20)
    ap.add_argument("--max-rsd", type=float, default=0.15)
    ap.add_argument("--min-runs", type=int, default=3)
    ap.add_argument("--ppl", type=float, default=360.0)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    try:
        res = fit(load(a.csv), tol=a.tol, max_rsd=a.max_rsd, min_runs=a.min_runs, ppl=a.ppl)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(render(res))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
