#!/usr/bin/env python3
"""Does the scripted demo moment survive a different start phase and a slower customer?

The dev host's scripted round ("a tap that was already running stops in the middle of a measurement", addon/challenge.py,
SCRIPTS["tap_stops"]) is anchored to what the hunt does: 12 s after the 4th pipe it closes. This runs it on a whole-second
timeline for every combination of

    start offset  0..9 s    (how long after the sensor's sampling grid the hunt starts)
    customer answer delay 0..40 s   (how long the person takes to say "it's quiet")

through the real service on a fake clock, and counts how often (a) a "first drop wins" engine names a pipe with no leak behind it
and (b) Leak Hunt does. It is a robustness check on the demo, not a measure of anything about real houses.

    python3 -I tools/phase_grid.py                 # about 5 minutes -> docs/check_phase_grid.json

Why it exists: on a timeline with sub-second phases the same script often does NOT fool the naive engine, because the hunt then
takes a different path (a measurement window that is exactly long enough at 9.0 s is one reading short at 8.7 s). The dev host
therefore runs on whole simulated seconds (addon/server.py, SteppedClock), the timeline every check in this repository uses.
Simulation only: fake clock, simulated house, simulated pipe controller.
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
warnings.simplefilter("ignore")

from addon.challenge import Challenge  # noqa: E402
from addon.service import HuntService  # noqa: E402
from addon.store import Store  # noqa: E402
from huntkit import Config  # noqa: E402

T0 = 1_800_000_000.0 + 23 * 3600
STARTS = list(range(0, 10))
REACTIONS = list(range(0, 41))


class Clock:
    def __init__(self): self.t = T0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def play(start_offset: float, reaction_s: float, max_s: int = 1000) -> dict:
    clock = Clock()
    svc = HuntService(Store(), clock, Config(), key=b"grid", public_base_url="https://x.test")
    svc.ensure_demo_household("h", control=True)
    ch = Challenge(svc, "h")
    ch.new_round(script="tap_stops")
    clock.advance(start_offset)
    hid = svc.start_hunt("h", mode="auto", consent=True, resume=False)["hunt_id"]
    for _ in range(max_s):
        clock.advance(1.0)
        svc.tick()
        v = svc.hunt_status("h", hid)
        if v["status"] != "active":
            break
        if v["auto"] and v["auto"]["needs_you"] and v["action"]["kind"] == "quiet":
            clock.advance(reaction_s)
            svc.report("h", hid, "quiet")
    r = ch.reveal()
    return dict(start=start_offset, reaction=reaction_s, running=r["running"], leak_hunt=r["engine"]["grade"], naive=r["naive"]["grade"],
                outside_changes=r["outside_changes"])


def main() -> None:
    t = time.time()
    rows = [play(s, r) for s in STARTS for r in REACTIONS]
    res = dict(runs=len(rows), start_offsets_s=[STARTS[0], STARTS[-1]], customer_delays_s=[REACTIONS[0], REACTIONS[-1]],
               timeline="whole seconds", outside_changes_per_hunt=sorted({r["outside_changes"] for r in rows}),
               naive_false_accusations=sum(r["naive"] == "false_accusation" for r in rows),
               leak_hunt_false_accusations=sum(r["leak_hunt"] == "false_accusation" for r in rows),
               leak_hunt_right=sum(r["leak_hunt"] == "right" for r in rows),
               still_running_at_the_end=sum(1 for r in rows if r["running"]), seconds=round(time.time() - t))
    print(json.dumps(res, indent=1))
    with open(os.path.join(ROOT, "docs", "check_phase_grid.json"), "w") as f:
        json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
