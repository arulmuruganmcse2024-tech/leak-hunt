#!/usr/bin/env python3
"""Reproduce the headline behaviour of Leak Hunt from a clean checkout, in about a minute, with no network and no hardware.

    python3 -I tools/repro.py            # runs 8 scenarios twice, prints them, writes docs/repro_output.{txt,json}
    python3 -I tools/repro.py --check    # same, and exits 1 if the result differs from the committed docs/repro_output.json

Everything runs the real service (addon/service.py) and the real engine (huntkit/) on a simulated house, a simulated or
emulated pipe controller and a fake clock, so every line is the same on every run. Nothing here touches water, a sensor or
a valve. What is printed under "Alexa:" is the exact `speech` string the service returns, the same string the on-screen
console shows.

Scenarios
  1  a normal hunt: the customer says yes, the leak is isolated, Alexa asks before keeping it closed, evidence replays
  2  a "first drop wins" engine is fooled by one unlucky outside change; Leak Hunt is not
  3  the house will not hold still: it says so, stops, puts everything back
  4  "stop" in the middle of a hunt
  5  the sensor's data stops: pause at 35 s, abort at 60 s, nothing opened or renewed in between
  6  the add-on dies with pipes closed: the pipe controller's own timer reopens them (real HTTP, emulated controller)
  7  an evidence packet that has been altered no longer verifies
  8  "fill the bath, 120 litres": water already moving is left out of the count and offered as a leak hunt; then the add-on
     dies near the end of a second fill and the pipe controller's own timer still closes the tap at about the amount
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import types
from importlib import metadata

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import addon.service as service_mod  # noqa: E402
from addon.actuators import CLOSED, OPEN  # noqa: E402
from addon.challenge import Challenge  # noqa: E402
from addon.service import LEASE_S, STALE_ABORT_S, HuntService  # noqa: E402
from addon.store import Store  # noqa: E402
from huntkit import Config  # noqa: E402
from pipehub_emulator import PipeHubEmulator  # noqa: E402

T0 = 1_800_000_000.0 + 23 * 3600


class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class _Ids:                                     # hunt ids are random in production; fixed here so every run prints the same text
    n = 0

    @classmethod
    def uuid4(cls):
        cls.n += 1
        return types.SimpleNamespace(hex=hashlib.sha256(b"repro-%d" % cls.n).hexdigest()[:32])


service_mod.uuid = _Ids


def make(sources=None, control=True):
    _Ids.n = 0
    clock = Clock()
    svc = HuntService(Store(), clock, Config(), key=b"repro-key", public_base_url="https://x.test")
    svc.ensure_demo_household("h", sources={"toilet": 2.0} if sources is None else sources, control=control)
    return svc, clock


def human_step(svc, hid, v):
    a = v["action"]
    if a["kind"] == "quiet":
        svc.clock.advance(3); svc.report("h", hid, "quiet")


def drive(svc, clock, hid, *, max_s=900, hook=None, until=None):
    """Tick once a second like the server's ticker. The customer only says "it's quiet"; the hub moves every pipe."""
    v = svc.hunt_status("h", hid)
    for i in range(max_s):
        clock.advance(1)
        if hook:
            hook(i, v)
        svc.tick()
        v = svc.hunt_status("h", hid)
        if v["status"] != "active" or (until and until(v)):
            return v
        if v["auto"] and v["auto"]["needs_you"]:
            human_step(svc, hid, v)
    return v


class Scene:
    def __init__(self, sid, title):
        self.sid, self.title, self.lines, self.facts = sid, title, [], {}

    def say(self, who, text): self.lines.append(f"{who}: {text}")
    def note(self, text): self.lines.append(f"-- {text}")
    def out(self): return dict(id=self.sid, title=self.title, lines=self.lines, facts=self.facts)


def physical(svc): return sorted(svc._source("h").physical_closed())


# ------------------------------------------------------------------------------------------------------------ scenarios
def s1():
    sc = Scene(1, "A normal hunt: yes, isolate, ask before keeping it closed, replayable evidence")
    svc, clock = make()
    sc.say("Customer", "Alexa, find my leak.")
    sc.say("Alexa", svc.start_hunt("h")["speech"])
    sc.say("Customer", "Yes, you run it.")
    v = svc.start_hunt("h", mode="auto", consent=True, resume=False)
    sc.say("Alexa", v["speech"])
    hid = v["hunt_id"]
    v = drive(svc, clock, hid)
    sc.say("Alexa", v["speech"])
    sc.facts.update(outcome=v["outcome"], found=v["found"], closed_while_asking=physical(svc),
                    latched_before_yes=[l["pipe"] for l in svc.store.latches("h")])
    sc.say("Customer", "Yes, keep the toilet off.")
    clock.advance(3)
    sc.say("Alexa", svc.set_pipe("h", "toilet", "off")["speech"])
    sc.facts["latched_after_yes"] = [l["pipe"] for l in svc.store.latches("h")]
    rows = svc.store.actuations("h", hid)
    sc.facts["valve_commands"] = len(rows)
    sc.facts["every_close_has_a_lease"] = all(r["detail"].startswith("lease") for r in rows if r["want"] == "closed")
    pk = svc.evidence("h", hid)
    sc.facts["evidence_replays_to_the_same_result"] = svc.verify_packet(pk)["replay_matches"]
    sc.facts["evidence_sha256"] = pk["sha256"]
    return sc.out()


def s2():
    sc = Scene(2, "One unlucky outside change: a 'first drop wins' engine accuses a dry pipe, Leak Hunt does not")
    svc, clock = make()
    ch = Challenge(svc, "h")
    ch.new_round(script="tap_stops")
    sc.note("hidden: one leak in this simulated house; a tap is already running; it will stop once, 12 s after the 4th pipe is closed")
    v = svc.start_hunt("h", mode="auto", consent=True, resume=False)
    v = drive(svc, clock, v["hunt_id"], max_s=500)
    r = ch.reveal()
    sc.say("Alexa", v["speech"])
    sc.note("reveal: " + ", ".join(f"{t['label']} {t['lpm']} L/min" for t in r["truth"]))
    sc.note("Leak Hunt isolated the flow to the water behind: " + ", ".join(r["engine"]["found"]) + f"  [{r['engine']['grade']}]")
    sc.note("a 'first drop wins' engine on the same readings accuses: " + ", ".join(r["naive"]["found"])
            + f"  [{r['naive']['grade']}: {', '.join(r['naive']['wrong'])} has no leak]")
    sc.note(r["verdict"])
    sc.facts.update(hidden=[t["label"] for t in r["truth"]], leak_hunt=r["engine"]["found"], leak_hunt_grade=r["engine"]["grade"],
                    naive=r["naive"]["found"], naive_grade=r["naive"]["grade"], outside_changes=r["outside_changes"],
                    within_claimed_bound=r["within_claimed_bound"])
    return sc.out()


def s3():
    sc = Scene(3, "The house will not hold still: it says so instead of guessing")
    svc, clock = make({"kitchen2": 1.73})
    ch = Challenge(svc, "h")
    ch.new_round(8)
    sc.note("hidden: a kitchen leak. Thrown at it: a tap on at 100 s and off at 140 s (two outside changes, beyond the one-change bound)")
    v = svc.start_hunt("h", mode="auto", consent=True, resume=False)
    t0, ev = clock(), [(100, "ambient_on"), (140, "ambient_off")]

    def hook(i, v):
        while ev and ev[0][0] <= clock() - t0:
            ch.act(ev.pop(0)[1])
    v = drive(svc, clock, v["hunt_id"], hook=hook, max_s=600)
    r = ch.reveal()
    sc.say("Alexa", v["speech"])
    sc.note(r["verdict"])
    sc.facts.update(outcome=v["outcome"], leak_hunt_found=r["engine"]["found"], leak_hunt_grade=r["engine"]["grade"],
                    still_closed=physical(svc), outside_changes=r["outside_changes"])
    return sc.out()


def s4():
    sc = Scene(4, "'Stop' in the middle of a hunt")
    svc, clock = make()
    v = svc.start_hunt("h", mode="auto", consent=True, resume=False)
    hid = v["hunt_id"]
    drive(svc, clock, hid, until=lambda v: len(physical(svc)) >= 1)
    sc.facts["closed_when_stop_was_said"] = physical(svc)
    sc.say("Customer", "Alexa, stop.")
    out = svc.stop_hunt("h")
    sc.say("Alexa", out["speech"])
    sc.facts.update(status=out["status"], closed_after_stop=physical(svc), unconfirmed=out["auto"]["not_restored"])
    return sc.out()


def s5():
    sc = Scene(5, "The sensor's data stops: pause at 35 s, abort at 60 s, nothing moved in between")
    svc, clock = make()
    ch = Challenge(svc, "h")
    ch.new_round(1)
    v = svc.start_hunt("h", mode="auto", consent=True, resume=False)
    hid = v["hunt_id"]
    drive(svc, clock, hid, until=lambda v: len(physical(svc)) >= 1)
    ch.act("blackout_on")
    closed_at_cut = physical(svc)
    sc.note("the sensor's link drops while " + ", ".join(closed_at_cut) + " is closed on a lease")
    paused_age = n_at_pause = None
    t_cut = clock()
    for _ in range(120):
        clock.advance(1); svc.tick()
        v = svc.hunt_status("h", hid)
        if paused_age is None and v["status"] == "active" and v["auto"]["paused"]:
            paused_age, n_at_pause = round(v["data_age_s"]), len(svc.store.actuations("h", hid))
            sc.say("Alexa", v["speech"])
        if v["status"] != "active":
            break
    sc.say("Alexa", v["speech"])
    after = sorted(svc.store.actuations("h", hid), key=lambda r: (r["t"], r["id"]))[n_at_pause:]
    sc.facts.update(
        paused_when_the_data_was_this_old_s=paused_age, stale_limit_s=Config().stale_s,
        aborted_when_the_data_was_this_old_s=round(v["data_age_s"]) if v.get("data_age_s") is not None else None,
        abort_limit_s=STALE_ABORT_S, outcome=v["outcome"],
        commands_between_the_pause_and_the_abort=[(r["source"], r["pipe"], r["want"]) for r in after if r["source"] != "hunt-end"],
        restore_at_the_abort=[(r["source"], r["pipe"], r["want"]) for r in after if r["source"] == "hunt-end"],
        closed_after=physical(svc))
    return sc.out()


def s6():
    sc = Scene(6, "The add-on dies with pipes closed: the pipe controller's own timer reopens them")
    clock = Clock()
    _Ids.n = 0
    svc = HuntService(Store(), clock, Config(), key=b"repro-key", public_base_url="https://x.test")
    svc.ensure_demo_household("h", sources={"toilet": 2.0}, control=False)
    emu = PipeHubEmulator(["bath", "toilet", "shower", "kitchen1", "kitchen2", "garden"], "tok", clock=clock,
                          on_change=lambda p, c, at: svc._source("h").set_closed(p, c, at)).start()
    try:
        svc.configure_pipehub("h", {"kind": "rest", "enabled": True, "base_url": emu.base_url, "token": "tok"})
        v = svc.start_hunt("h", mode="auto", consent=True, resume=False)
        hid = v["hunt_id"]
        clock.advance(3); svc.report("h", hid, "quiet")
        for _ in range(300):
            clock.advance(1); svc.tick()
            if len(emu.closed_pipes()) >= 1:
                break
        sc.facts["closed_when_the_add_on_died"] = emu.closed_pipes()
        sc.note("the add-on process is gone from here on: no tick, no renew, no status call. Only the controller's clock runs.")
        t_dead = clock()
        for _ in range(int(LEASE_S) + 5):
            clock.advance(1)
        sc.facts.update(closed_after_the_lease_ran_out=emu.closed_pipes(),
                        longest_any_pipe_was_closed_s=round(max(emu.max_closed_run_s(p) for p in ("bath", "toilet", "shower", "kitchen1", "kitchen2", "garden")), 1),
                        lease_s=LEASE_S, silent_for_s=round(clock() - t_dead))
        sc.say("Controller", "every pipe is open again, reopened by its own lease timer")
    finally:
        emu.stop()
    return sc.out()


def s7():
    sc = Scene(7, "An evidence packet that has been altered no longer verifies")
    svc, clock = make()
    v = svc.start_hunt("h", mode="auto", consent=True, resume=False)
    drive(svc, clock, v["hunt_id"])
    pk = svc.evidence("h", v["hunt_id"])
    sc.facts["untouched"] = svc.verify_packet(pk)
    bad = json.loads(json.dumps(pk))
    bad["readings"][len(bad["readings"]) // 2]["total_l"] += 3.0
    sc.facts["one_reading_changed"] = svc.verify_packet(bad)
    sc.note("the packet proves integrity and that the same readings replay to the same result; it does not prove the diagnosis is true")
    return sc.out()


def s8():
    sc = Scene(8, "Fill the bath by volume: the controller's timer is the fill timer")
    clock = Clock()
    svc = HuntService(Store(), clock, Config(), key=b"repro-key", public_base_url="https://x.test")
    svc.ensure_demo_household("h", sources={"toilet": 2.0}, control=True, outlets=True)
    src, hub = svc._source("h"), svc._hub("h")
    clock.advance(30); svc.tick()
    leases = []
    real = hub.command

    def spy(pipe, want, now, lease_s):
        if pipe == "tub" and want == OPEN:
            leases.append(lease_s)
        return real(pipe, want, now, lease_s)
    hub.command = spy
    sc.say("Customer", "Alexa, fill the bath, 120 litres.")
    p = svc.start_fill("h", "bath", 120)
    sc.say("Alexa", p["speech"])
    open_unleased = 0
    for _ in range(1500):
        clock.advance(1); svc.tick()
        st = hub.pipes(clock())["tub"]
        open_unleased += st.position == OPEN and st.lease_left_s is None
        p = svc.fill_status("h")
        if p["status"] != "active":
            break
    sc.say("Alexa", p["speech"])
    first = svc.store.latest_fill("h")["state"]
    sc.facts.update(target_l=120, alexa_said_l=p["delivered_l"], really_delivered_l=round(src.delivered_l("tub", clock()), 1),
                    seconds_open_without_a_lease=open_unleased, longest_tap_lease_s=round(max(leases), 1),
                    paused_once_to_recheck=bool(first.get("rechecked")),
                    recheck_measured_elsewhere_lpm=first.get("baseline_lpm"))
    sc.note("second fill: the add-on stops 625 s in (about 30 s before the end); only the controller's own timer runs from then on")
    clock.advance(30); svc.tick()
    before = src.delivered_l("tub", clock())
    svc.start_fill("h", "bath", 120)
    t0 = clock()
    for _ in range(1500):
        clock.advance(1)
        if clock() - t0 < 625:
            svc.tick()
        else:
            hub.expire(clock())
        if clock() - t0 > 745:
            break
    sc.facts.update(dead_addon_really_delivered_l=round(src.delivered_l("tub", clock()) - before, 1), dead_addon_tap_closed=not src.outlet_open("tub"))
    sc.say("Controller", "the tap closed itself when the lease ran out, at about the amount")
    return sc.out()


SCENES = [s1, s2, s3, s4, s5, s6, s7, s8]


def source_hash() -> str:
    h = hashlib.sha256()
    for d in ("huntkit", "addon", "tools", "tests"):
        for dp, dn, fn in sorted(os.walk(os.path.join(ROOT, d))):
            dn[:] = sorted(x for x in dn if x != "__pycache__")
            for f in sorted(fn):
                if f.endswith((".py", ".html", ".sh")):
                    p = os.path.join(dp, f)
                    h.update(os.path.relpath(p, ROOT).encode() + b"\0" + open(p, "rb").read() + b"\0")
    return h.hexdigest()


def render(res) -> str:
    out = ["LEAK HUNT - reproduction (simulated house, fake clock, no hardware)", ""]
    for s in res["scenarios"]:
        out.append(f"=== {s['id']}. {s['title']}")
        out += ["  " + l for l in s["lines"]]
        out.append("  facts: " + json.dumps(s["facts"], sort_keys=True))
        out.append("")
    out.append("deterministic (two runs, byte-identical): " + ("yes" if res["deterministic"] else "NO"))
    return "\n".join(out) + "\n"


def run_all():
    return [f() for f in SCENES]


def main() -> int:
    check = "--check" in sys.argv
    a = run_all()
    b = run_all()
    res = dict(scenarios=a, deterministic=json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True))
    text = render(res)
    print(f"python {platform.python_version()} on {platform.system()}; mcp {metadata.version('mcp')}")
    print(f"source sha256 (huntkit, addon, tools, tests): {source_hash()}")
    print()
    print(text)
    docs = os.path.join(ROOT, "docs")
    path_json = os.path.join(docs, "repro_output.json")
    if check:
        try:
            old = json.load(open(path_json))
        except Exception:
            print("no committed docs/repro_output.json to compare with")
            return 1
        same = json.dumps(old["scenarios"], sort_keys=True) == json.dumps(a, sort_keys=True)
        print("matches the committed docs/repro_output.json: " + ("yes" if same else "NO"))
        return 0 if same and res["deterministic"] else 1
    with open(path_json, "w") as f:
        json.dump(res, f, indent=1, sort_keys=True)
    with open(os.path.join(docs, "repro_output.txt"), "w") as f:
        f.write(text)
    return 0 if res["deterministic"] else 1


if __name__ == "__main__":
    sys.exit(main())
