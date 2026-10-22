"""Overnight 'minimum night flow' analytics from cumulative volume counters.

The classic utility method: when nobody should be using water, any sustained flow is leakage.
We compute it from cumulative totals, so the 1-in-10 telemetry sampling loses nothing.

Honesty rules (same as the hunt engine):
  * only windows with full coverage, one boot, and an OPEN valve count;
  * a night of zero says 'nothing above the sensor floor', never 'no leak';
  * the trend claim is compared against the household's own previous nights.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .core import Config, Reading


@dataclass
class Night:
    date: str                       # local calendar date the quiet window starts on (YYYY-MM-DD)
    min_lpm: Optional[float]        # lowest sustained flow across the quiet window
    coverage: float                 # fraction of the quiet window with usable data
    n_windows: int
    sustained_from: Optional[float] = None   # epoch seconds when flow >= floor began and never stopped


@dataclass
class NightReport:
    nights: List[Night]
    latest: Optional[Night]
    baseline_median_lpm: Optional[float]
    verdict: str                    # 'insufficient_data' | 'normal' | 'new_overnight_flow' | 'ongoing_overnight_flow'
    text: str
    claims: List[dict] = field(default_factory=list)


def _local(t: float, tz_min: int) -> float:
    return t + tz_min * 60.0


def quiet_windows(readings: Sequence[Reading], tz_min: int, start_h: float, end_h: float) -> Dict[str, List[Reading]]:
    """Group readings by the local date on which the quiet window starts."""
    out: Dict[str, List[Reading]] = {}
    for r in readings:
        lt = _local(r.t, tz_min)
        day = int(lt // 86400)
        sec = lt - day * 86400
        if start_h * 3600 <= sec < end_h * 3600:
            out.setdefault(_date(day), []).append(r)
    return out


def _date(day_index: int) -> str:
    import datetime
    return (datetime.date(1970, 1, 1) + datetime.timedelta(days=day_index)).isoformat()


def night_min_flow(rs: Sequence[Reading], cfg: Config, *, window_s: float = 1200.0, max_gap_s: float = 60.0,
                   quiet_len_s: float = 4 * 3600.0) -> Night:
    rs = sorted(rs, key=lambda r: r.t)
    date = _date(int(_local(rs[0].t, 0) // 86400)) if rs else ""
    usable_s = 0.0
    wins: List[Tuple[float, float, float]] = []   # (t_start, t_end, lpm)
    i = 0
    while i < len(rs):
        j = i
        # extend over one boot, OPEN, no big gaps
        while j + 1 < len(rs):
            a, b = rs[j], rs[j + 1]
            if b.boot_id != a.boot_id or b.valve != "OPEN" or b.leak or (b.t - a.t) > max_gap_s \
                    or b.out_total_l < a.out_total_l:
                break
            j += 1
        seg = rs[i:j + 1]
        if len(seg) >= 2 and seg[0].valve == "OPEN" and not seg[0].leak:
            usable_s += seg[-1].t - seg[0].t
            # rolling windows of window_s inside the segment
            k = 0
            for m in range(len(seg)):
                while seg[m].t - seg[k].t > window_s and k < m:
                    k += 1
                if seg[m].t - seg[k].t >= window_s * 0.95:
                    dv = seg[m].out_total_l - seg[k].out_total_l
                    dt = seg[m].t - seg[k].t
                    wins.append((seg[k].t, seg[m].t, dv / (dt / 60.0)))
        i = j + 1
    cov = min(1.0, usable_s / quiet_len_s) if quiet_len_s else 0.0
    if not wins:
        return Night(date=date, min_lpm=None, coverage=cov, n_windows=0)
    mn = min(w[2] for w in wins)
    # when did flow >= floor begin and never stop until the end of the night?
    sustained = None
    for w in reversed(wins):
        if w[2] >= cfg.floor_lpm:
            sustained = w[0]
        else:
            break
    return Night(date=date, min_lpm=max(0.0, mn), coverage=cov, n_windows=len(wins), sustained_from=sustained)


def analyse(readings: Sequence[Reading], cfg: Config, *, tz_min: int = 330, start_h: float = 1.0, end_h: float = 5.0,
            min_nights: int = 3) -> NightReport:
    groups = quiet_windows(readings, tz_min, start_h, end_h)
    nights: List[Night] = []
    for date in sorted(groups):
        n = night_min_flow(groups[date], cfg, quiet_len_s=(end_h - start_h) * 3600.0)
        n.date = date
        nights.append(n)
    good = [n for n in nights if n.min_lpm is not None and n.coverage >= 0.6]
    if len(good) < 2:
        return NightReport(nights, nights[-1] if nights else None, None, "insufficient_data",
                           "I don't have enough complete overnight data yet to compare nights.")
    latest = good[-1]
    prev = good[:-1]
    med = statistics.median(n.min_lpm for n in prev)
    mad = statistics.median(abs(n.min_lpm - med) for n in prev) * 1.4826
    thr = med + max(3 * mad, 0.5 * cfg.floor_lpm)
    run = 0
    for n in reversed(good):
        if n.min_lpm >= cfg.floor_lpm:
            run += 1
        else:
            break
    if latest.min_lpm >= cfg.floor_lpm and latest.min_lpm > thr:
        if run >= 2:
            verdict = "ongoing_overnight_flow"
            before = ("your quietest hours stayed below what the sensor can read reliably" if med < cfg.floor_lpm
                      else f"your typical quiet-hours flow was {med:.1f} litres per minute")
            text = (f"Water has been moving all night for {run} nights in a row, at least "
                    f"{latest.min_lpm:.1f} litres per minute even in the quietest hours. Before that, {before}.")
        else:
            verdict = "new_overnight_flow"
            usual = ("below what the sensor can read reliably" if med < cfg.floor_lpm else f"a usual {med:.1f} litres per minute")
            text = (f"Last night the quietest stretch still had {latest.min_lpm:.1f} litres per minute flowing, "
                    f"against {usual}.")
    elif latest.min_lpm < cfg.floor_lpm:
        verdict = "normal"
        text = (f"Your quietest overnight flow was below what your sensor can read reliably "
                f"({cfg.floor_lpm:.1f} litres per minute). Nothing larger was running, but that says nothing about slow drips.")
    else:
        verdict = "normal"
        text = f"Last night's quietest flow was {latest.min_lpm:.1f} litres per minute, in line with your usual {med:.1f} litres per minute."
    claims = [dict(kind="night_min_flow", date=n.date, lpm=None if n.min_lpm is None else round(n.min_lpm, 3),
                   coverage=round(n.coverage, 2)) for n in nights[-14:]]
    return NightReport(nights, latest, med, verdict, text, claims)
