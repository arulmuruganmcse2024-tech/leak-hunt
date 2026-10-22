#!/usr/bin/env python3
"""Randomised end-to-end scenarios for the AUTOMATIC hunt, on the simulated house with a fake clock.

Each scenario draws a random house state (zero, one or two leaks, below-floor leaks), a random set of hand valves, a random
customer (quick, slow, absent, saying stop), random voice commands in the middle of a hunt, an ambient tap, and random faults
(a stuck valve, a hub that drops off, a hub reboot, a telemetry gap). It then ticks the service once a second like the
server's ticker and checks, after every tick and at the end:

  A1  the moment a hunt ends (hub reachable), and in any case once any hub outage + lease time has passed, every pipe the hub can move is open, except
      a confirmed culprit held on a lease for the "keep it closed?" question, or one the customer said yes to
  A2  no pipe is ever closed without a live lease, unless it is a latched culprit or the customer's own voice "off"
  A3  no false accusation when there was at most ONE ambient flow change and no fault (the bound the engine proves);
      scenarios outside that bound are counted separately and not treated as failures
  A4  only pipes the hub can move are ever commanded; protected pipes and hand valves never
  A5  every hunt ends: nothing stays 'active' past the idle timeout
  A6  "stop" puts every movable pipe back immediately (hub reachable) and the hunt reads as stopped
  A7  a hunt never exceeds its command allowance (plus the restore moves)
  A8  a pipe is never flipped twice inside the minimum interval, except forced restores and lease renewals
  A9  no hunt keeps running on telemetry older than the stale limit
  A10 no NEW valve command is sent while telemetry is older than the stale threshold (the hunt is paused)
  A11 a pipe is never latched except after the customer's explicit yes to "keep it closed?" within the question window
  A12 an unanswered or declined question leaves nothing closed, latched or offered after its lease
  A13 whatever is still closed when a hunt ends and is not a held/kept culprit is named in `auto.not_restored`, and nothing
      listed there is actually open
  A14 a hunt that holds a pipe closed keeps making progress (a valve step, a report, a question to the customer) at least every
      150 s, unless it is paused on stale data or the hub is down. (Found with the challenge search: an outside tap that stopped
      in the middle of a step left the hunt "re-measuring" nothing for 30 minutes with a pipe closed.)

AUTO_FUZZ_JITTER=1 runs the same scenarios on a timeline with sub-second phases instead of whole seconds: the hunt starts a random
fraction of a second off the sensor's sampling grid and every tick lasts between 0.8 and 1.3 s. Whole-second timelines can hide
paths the engine only takes at other phases (a measurement window that is exactly long enough at 9.0 s is one reading short at 8.7 s).

AUTO_FUZZ_MUTANT=<name> breaks the service on purpose to prove these checks can fail:
  no_lease (closes carry no lease)   no_restore (nothing is put back at the end)   hand_valves (tries to move hand valves)
  stale_ignored (never aborts on stale data)   stale_acts (keeps renewing leases on stale data)
  offer_unleased (a culprit is held with neither lease nor question)   auto_latch (a culprit is latched without a yes)
  no_remeasure (an unclean window is waited on instead of measured again)

Everything is SIMULATION: VirtualPipeHub + VirtualHouse. It checks the service's policy, not any real valve.

usage: python3 tools/auto_fuzz.py [scenarios=300] [seed0=0] [out=docs/check_auto_fuzz.json]
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import Counter

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import addon.service as service_mod  # noqa: E402
from addon.service import HuntService, LEASE_S, MAX_COMMANDS, MIN_PIPE_INTERVAL_S, STALE_ABORT_S, UserError  # noqa: E402
from addon.store import Store  # noqa: E402
from huntkit import Config  # noqa: E402
from huntkit.sim import demo_topology  # noqa: E402

T0 = 1_800_000_000.0 + 23 * 3600
NO_PROGRESS_S = 150.0                   # A14
ASKED = Counter()                       # how often Alexa had to ask "which one?" before the hunt could start
LEAVES = ["toilet", "shower", "kitchen1", "kitchen2", "garden"]


class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def voice_closed(svc, pipe: str) -> bool:
    """The customer's own spoken 'off' is theirs to keep: the last successful command for this pipe was a voice close."""
    rows = [a for a in svc.store.actuations("h") if a["pipe"] == pipe and a["ok"]]
    return bool(rows and rows[0]["source"] == "voice" and rows[0]["want"] == "closed")


def draw(seed: int) -> dict:
    r = random.Random(seed)
    n_leaks = r.choices([0, 1, 2], [2, 6, 2])[0]
    leaks = {n: round(r.uniform(1.4, 4.0), 2) for n in r.sample(LEAVES, n_leaks)}
    if r.random() < 0.08:
        leaks = {r.choice(LEAVES): 0.4}                                  # below what the sensor reads reliably
    manual = r.sample(["bath", "toilet", "shower", "kitchen1", "kitchen2", "garden"], r.choice([0, 0, 0, 1, 1, 2]))
    ambient = []
    k = r.choices([0, 1, 2], [6, 3, 1])[0]
    if k:
        ambient = sorted([(r.uniform(60, 400), round(r.uniform(1.2, 3.0), 2) if i % 2 == 0 else 0.0) for i in range(k)])
    faults = []
    if r.random() < 0.30:
        kind = r.choice(["stuck", "offline", "reboot", "telemetry_gap", "telemetry_gap"])
        if kind == "stuck":
            faults.append(dict(kind="stuck", pipe=r.choice(LEAVES + ["bath"]), t=r.choice([0.0, r.uniform(30, 300)])))
        elif kind == "offline":
            faults.append(dict(kind="offline", t=r.uniform(30, 300), dur=r.choice([5, 10, 20, 90, 400])))
        elif kind == "reboot":
            faults.append(dict(kind="reboot", t=r.uniform(40, 300)))
        else:
            faults.append(dict(kind="telemetry_gap", t=r.uniform(30, 150), dur=r.choice([20, 45, 55, 90, 200])))   # while pipes are closed
    human = r.choices(["quick", "slow", "absent"], [7, 2, 1])[0]
    voice = []
    if r.random() < 0.25:
        voice.append(dict(t=r.uniform(50, 300), pipe=r.choice(["kitchen", "kitchen pipe 2", "toilet", "garden tap", "sink", "everything"]),
                          state=r.choice(["on", "off"]), confirm=r.random() < 0.5))
    stop = r.uniform(40, 400) if r.random() < 0.15 else None
    return dict(seed=seed, leaks=leaks, manual=manual, ambient=ambient, faults=faults, human=human, voice=voice, stop=stop,
                suspect=r.choice([None, None, ["toilet"], ["kitchen"]]),
                isolation=r.choice(["yes", "yes", "no", "silent", "late_yes"]), iso_delay=round(r.uniform(4, 100), 1))


def run(sc: dict, max_s: float = 2100.0) -> dict:
    clock = Clock()
    svc = HuntService(Store(), clock, Config(), key=b"fuzz", public_base_url="https://x.test")
    svc.ensure_demo_household("h", sources=dict(sc["leaks"]), control=True, manual=sc["manual"])
    mut = os.environ.get("AUTO_FUZZ_MUTANT", "")
    actuated = set(svc._actuated("h", svc._topology("h")))           # the truth, taken BEFORE any deliberate breakage below
    if mut == "no_lease":
        orig_cmd = svc._command
        svc._command = lambda hid_, hunt_id, pipe, want, source, lease_s=None, **kw: orig_cmd(hid_, hunt_id, pipe, want, source, None, **kw)
    elif mut == "no_restore":
        svc._on_end = lambda *a, **k: None
    elif mut == "hand_valves":
        svc._actuated = lambda hid_, topo_: list(topo_.nodes)
    elif mut == "stale_ignored":
        service_mod.STALE_ABORT_S = 1e9
    elif mut == "offer_unleased":
        orig_cmd2 = svc._command
        svc._command = lambda hid_, hunt_id, pipe, want, source, lease_s=None, **kw: orig_cmd2(
            hid_, hunt_id, pipe, want, source, None if source == "hunt-end" else lease_s, **kw)
        svc.store.set_offer = lambda *a, **k: None
    elif mut == "auto_latch":
        orig_end = svc._on_end
        def latching_end(hunt, outcome, js, t):
            orig_end(hunt, outcome, js, t)
            for o in list(svc.store.offers(hunt["hid"], svc.clock())):
                svc._command(hunt["hid"], hunt["id"], o["pipe"], "closed", "hunt-end", None, force=True)
                svc.store.set_latch(hunt["hid"], o["pipe"], t, hunt["id"], o["lpm"])
                svc.store.clear_offer(hunt["hid"], o["pipe"])
        svc._on_end = latching_end
    elif mut == "no_remeasure":
        service_mod.REMEASURE_WHY = ()
    elif mut == "stale_acts":                                        # lease renewals go on while the data is stale
        orig_vr = svc._verify_and_renew
        svc._verify_and_renew = lambda hunt, v, pos, acts, now, renew=True: orig_vr(hunt, v, pos, acts, now, renew=True)
    hub, src = svc._hub("h"), svc._source("h")
    topo = svc._topology("h")
    wire: list = []                                                  # every command that reached the hub, renewals included
    orig_hub_command = hub.command

    def logged_command(pipe, want, now, lease_s):
        wire.append((now, pipe, want))
        return orig_hub_command(pipe, want, now, lease_s)
    hub.command = logged_command
    viol: list[str] = []
    note = lambda code, msg: viol.append(f"{code} {msg}")
    gap = {"on": None}
    orig_poll = src.poll
    src.poll = lambda now: [] if (gap["on"] and gap["on"][0] <= now < gap["on"][1]) else orig_poll(now)
    stuck_at = []

    def set_ambient(lpm: float):
        src._sample_until(clock())
        src.house.advance(max(0.0, clock() - src.house.t))
        src.house.extra_lpm = lpm

    ambient_events = list(sc["ambient"])
    fault_events = [dict(f) for f in sc["faults"]]
    voice_events = [dict(v) for v in sc["voice"]]
    stop_at = sc["stop"]
    hunt_ids: list[str] = []
    latch_allowed: set = set()              # pipes the customer explicitly said yes to keeping closed
    stale_count = None                      # number of valve commands when the hunt first went stale
    stopped_called = False

    def not_restored_check(out: dict) -> None:
        """A13, at the moment a hunt has ended: what is still closed (and not a held/kept culprit) must be named, and only that."""
        held = {o["pipe"] for o in svc.store.offers("h", clock())} | {l["pipe"] for l in svc.store.latches("h")}
        still = {p for p in actuated & src.physical_closed() if p not in held and not voice_closed(svc, p)}
        listed = {r["id"] for r in ((out.get("auto") or {}).get("not_restored") or [])}
        if still - listed:
            note("A13", f"still closed and not reported: {sorted(still - listed)}")
        wrong = [p for p in listed if p not in src.physical_closed()]
        if wrong:
            note("A13", f"reported as not restored but open: {wrong}")

    jit = os.environ.get("AUTO_FUZZ_JITTER") == "1"
    jr = random.Random(int(sc["seed"]) * 7919 + 13)

    def dt() -> float:
        return jr.uniform(0.8, 1.3) if jit else 1.0

    if jit:
        clock.advance(jr.uniform(0.05, 0.95))                      # the hunt starts off the sensor's sampling grid
    t_start = clock()
    try:
        try:
            v = svc.start_hunt("h", mode="auto", consent=True, suspect=sc["suspect"], resume=False)
        except UserError as e:
            if not str(e).startswith("Which one"):
                raise
            ASKED["which_pipe"] += 1                       # Alexa asked "which one?"; the customer answers "two"
            v = svc.start_hunt("h", mode="auto", consent=True, suspect=["kitchen pipe 2"], resume=False)
    except UserError as e:
        return dict(sc=sc, outcome="refused:" + str(e)[:40], violations=[], in_bound=False, found=[], false_accusation=False)
    hid = v["hunt_id"]; hunt_ids.append(hid)
    human_due = None; replied_quiet = False
    prog = {"sig": None, "t": clock(), "noted": False}               # A14
    pipes_closed_run: dict = {}
    offline_until = 0.0
    last_ok: dict = {}
    end_t = None
    for _ in range(int(max_s)):
        clock.advance(dt())
        t = clock() - t_start
        while ambient_events and ambient_events[0][0] <= t:
            set_ambient(ambient_events.pop(0)[1])
        for f in list(fault_events):
            if f["t"] <= t:
                fault_events.remove(f)
                if f["kind"] == "stuck":
                    hub.stuck[f["pipe"]] = "closed" if f["pipe"] in src.physical_closed() else "open"
                elif f["kind"] == "offline":
                    hub.offline = True; offline_until = t + f["dur"]
                elif f["kind"] == "reboot":
                    hub.reboot(clock())
                elif f["kind"] == "telemetry_gap":
                    gap["on"] = (clock(), clock() + f["dur"])
        if hub.offline and t >= offline_until:
            hub.offline = False
        for ve in list(voice_events):
            if ve["t"] <= t:
                voice_events.remove(ve)
                try:
                    r = svc.set_pipe("h", ve["pipe"], ve["state"], all_in_room=True)
                    if r.get("kind") == "needs_confirm" and ve["confirm"]:
                        svc.set_pipe("h", ve["pipe"], ve["state"], all_in_room=True, confirm=True)
                except UserError:
                    pass
        if stop_at is not None and t >= stop_at and not stopped_called:
            stopped_called = True
            stop_out = svc.stop_hunt("h")
            if stop_out.get("status") == "stopped":
                not_restored_check(stop_out)
            if not hub.offline:
                bad = [p for p in actuated if p in src.physical_closed() and p not in hub.stuck and not voice_closed(svc, p)]
                if bad:
                    note("A6", f"after stop these pipes are still closed: {bad}")
        try:
            svc.tick()
            v = svc.hunt_status("h", hid)
        except Exception as e:                                      # the service must never raise out of a tick
            note("A5", f"tick/hunt_status raised {type(e).__name__}: {e}")
            break
        if v["status"] == "active" and v.get("data_age_s") is not None and v["data_age_s"] > STALE_ABORT_S + 5:
            note("A9", f"hunt still active on {v['data_age_s']:.0f} s old telemetry at t={t:.0f}")
        # --- A10: paused while stale -> no valve command reaches the hub (renewals included)
        n_cmds = len(wire)
        age = v.get("data_age_s")
        if v["status"] == "active" and age is not None and age > Config().stale_s + 0.5:
            if stale_count is None:
                stale_count = n_cmds
            elif n_cmds > stale_count:
                note("A10", f"{n_cmds - stale_count} valve command(s) reached the hub while the data was stale at t={t:.0f}: {wire[stale_count:]}")
                stale_count = n_cmds
        else:
            stale_count = None
        # --- A11: nothing is latched without a yes
        extra = {l["pipe"] for l in svc.store.latches("h")} - latch_allowed
        if extra:
            note("A11", f"{sorted(extra)} latched without the customer's yes")
            latch_allowed |= extra
        # --- A14: a hunt that holds a pipe closed keeps moving unless it is waiting for the customer or paused
        sig = (svc.store.count_actuations(hid), len(svc.store.reports(hid)))
        if (v["status"] != "active" or sig != prog["sig"] or (v["auto"] and v["auto"]["needs_you"]) or hub.offline
                or (age is not None and age > Config().stale_s + 0.5)):
            prog["sig"], prog["t"] = sig, clock()
        elif clock() - prog["t"] > NO_PROGRESS_S and (actuated & src.physical_closed()) and not prog["noted"]:
            prog["noted"] = True
            note("A14", f"no step, report or question for {clock() - prog['t']:.0f} s while {sorted(actuated & src.physical_closed())} "
                        f"was held closed at t={t:.0f}")
        # --- A2: every closed, movable pipe is backed by a lease (or is a latched culprit, or the customer's own voice 'off')
        closed = src.physical_closed()
        latched = {l["pipe"] for l in svc.store.latches("h")}
        for p in actuated & closed:
            if p in latched or p in hub.stuck:
                continue
            until = hub.lease_until.get(p)
            if until is not None and until > clock() - 0.5:
                continue
            if voice_closed(svc, p):
                continue
            note("A2", f"{p} closed with no live lease at t={t:.0f}")
        # --- the customer
        if v["status"] == "active" and v["auto"] and v["auto"]["needs_you"]:
            if sc["human"] == "absent":
                pass
            else:
                if human_due is None:
                    human_due = clock() + (random.Random(sc["seed"] + int(t)).uniform(1, 4) if sc["human"] == "quick" else
                                           random.Random(sc["seed"] + int(t)).uniform(8, 30))
                if clock() >= human_due:
                    human_due = None
                    a = v["action"]
                    if a["kind"] == "quiet":
                        svc.report("h", hid, "quiet")
                    else:
                        for n in a["close"]:
                            svc.report("h", hid, "close", n)
                        for n in a["open"]:
                            svc.report("h", hid, "open", n)
        else:
            human_due = None
        if v["status"] != "active":
            end_t = clock()
            break
    # --- end of the run
    v = svc.hunt_status("h", hid)
    held_at_end = {o["pipe"] for o in svc.store.offers("h", clock())}
    if v["status"] != "active" and not hub.offline:                 # the moment a hunt ends, with the hub reachable, the pipes are back
        latched_now = {l["pipe"] for l in svc.store.latches("h")}
        now_left = [p for p in actuated & src.physical_closed()
                    if p not in latched_now and p not in held_at_end and p not in hub.stuck and not voice_closed(svc, p)]
        if now_left:
            note("A1", f"pipes still closed at the moment the hunt ended: {now_left}")
    if v["status"] != "active":
        not_restored_check(v)
        if held_at_end:
            if not v["auto"]["isolation_offer"]:
                note("A11", "a culprit is held on a lease but the result carries no question")
        elif v["auto"] and v["auto"]["isolation_offer"]:
            note("A11", "the result asks to keep a pipe closed that is not held")
    if v["status"] == "active":
        note("A5", f"hunt still active after {max_s:.0f} s (human={sc['human']})")
    # let any outage and every lease run out, with the hub reachable; the customer answers the "keep it closed?" question
    hub.offline = False
    iso = sc.get("isolation", "silent")
    answer_at = int(sc.get("iso_delay", 30)) if iso in ("yes", "no") else int(LEASE_S) + 20
    answered = False
    for k in range(int(LEASE_S) + 40):
        clock.advance(dt())
        if held_at_end and iso != "silent" and not answered and k >= answer_at:
            answered = True
            for p in sorted(held_at_end):
                try:
                    if iso == "yes":
                        svc.set_pipe("h", topo.label(p), "off")
                        if p in {l["pipe"] for l in svc.store.latches("h")}:
                            latch_allowed.add(p)
                        else:
                            note("A11", f"a yes to {p} (inside the window) did not latch it")
                    elif iso == "no":
                        svc.set_pipe("h", topo.label(p), "on")
                    else:                                              # late_yes: after the lease lapsed it is an ordinary 'off'
                        svc.set_pipe("h", topo.label(p), "off")
                except UserError:
                    pass
        try:
            svc.tick()
        except Exception as e:
            note("A5", f"tick raised after the hunt {type(e).__name__}")
            break
    if v["status"] != "active":
        latched = {l["pipe"] for l in svc.store.latches("h")}
        extra = latched - latch_allowed
        if extra:
            note("A11", f"{sorted(extra)} latched without a yes")
        if svc.store.offers("h", clock()):
            note("A12", "an offer outlived its window")
        stray = [p for p in actuated & src.physical_closed() if p not in latched and p not in hub.stuck and not voice_closed(svc, p)]
        if stray:
            note("A1", f"pipes left closed after the hunt ended: {stray}")
        if iso in ("no", "silent") and held_at_end:
            left = [p for p in held_at_end if p in src.physical_closed() and p not in hub.stuck]
            if left:
                note("A12", f"a declined or unanswered question left {left} closed")
    # --- A4 / A7 / A8 from the audit log
    acts = [a for h in hunt_ids for a in svc.store.actuations("h", h)] + [a for a in svc.store.actuations("h") if a["hunt_id"] is None]
    for a in acts:
        if a["pipe"] not in actuated:
            note("A4", f"{a['pipe']} was commanded but the hub cannot move it ({a['source']})")
        if topo.nodes[a["pipe"]].protect and a["want"] == "closed":
            note("A4", f"protected pipe {a['pipe']} was closed")
    for h in hunt_ids:
        n = len(svc.store.actuations("h", h))
        if n > MAX_COMMANDS + 2 * len(actuated) + 4:
            note("A7", f"{n} commands in one hunt")
    last = {}
    for a in sorted(acts, key=lambda x: x["t"]):
        if not a["ok"] or a["source"] in ("hunt-start", "hunt-end", "hunt-renew"):
            continue
        if a["pipe"] in last and a["t"] - last[a["pipe"]] < MIN_PIPE_INTERVAL_S - 1e-6:
            note("A8", f"{a['pipe']} flipped twice within {a['t'] - last[a['pipe']]:.1f} s")
        last[a["pipe"]] = a["t"]
    # --- A3
    found = list(v.get("found") or [])
    leak_leaves = {n for n, q in sc["leaks"].items() if q >= 1.0}
    in_bound = len(sc["ambient"]) <= 1 and not sc["faults"] and not sc["voice"] and sc["stop"] is None
    def has_leak(node):
        return any(l in leak_leaves for l in (topo.subtree_leaves(node) if not topo.is_leaf(node) else [node]))
    false_acc = any(not has_leak(n) for n in found)
    if false_acc and in_bound:
        note("A3", f"false accusation {found} with leaks {sc['leaks']} ambient {sc['ambient']}")
    return dict(sc=sc, outcome=str(v.get("outcome")), status=v["status"], found=found, violations=viol, in_bound=in_bound,
                false_accusation=false_acc, commands=len(acts), virtual_s=round(clock() - t_start))


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    seed0 = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(ROOT, "docs", "check_auto_fuzz.json")
    t0 = time.time()
    outcomes, bad, refusals = Counter(), [], Counter()
    in_bound = right = out_false = 0
    codes = Counter()
    found_correct = 0
    for i in range(n):
        sc = draw(seed0 + i)
        r = run(sc)
        outcomes[r["outcome"].split(":")[0] + (":" + r["outcome"].split(":")[1] if r["outcome"].startswith("aborted") and ":" in r["outcome"] else "")] += 1
        if r["outcome"].startswith("refused:"):
            refusals[r["outcome"][8:]] += 1
        if r["in_bound"]:
            in_bound += 1
            if r["found"]:
                found_correct += 1
        elif r["false_accusation"]:
            out_false += 1
        for v in r["violations"]:
            codes[v.split()[0]] += 1
        if r["violations"]:
            bad.append(dict(seed=sc["seed"], violations=r["violations"][:4], scenario=sc))
        if (i + 1) % 25 == 0:
            print(f"{i + 1}/{n} scenarios, violations so far: {sum(codes.values())}, {time.time() - t0:.0f}s", flush=True)
    # fixed probes: a long search (leak in the last pipe tried) whose telemetry drops while pipes are closed, so that a lease
    # renewal falls inside the stale window (A10) and, for the long gaps, the abort (A9)
    probes = 0
    for k, (t_gap, dur) in enumerate([(50, 55), (60, 55), (70, 55), (50, 200), (80, 90)]):
        sc = draw(900_000 + k)
        sc.update(leaks={"garden": 2.5}, manual=[], ambient=[], faults=[dict(kind="telemetry_gap", t=float(t_gap), dur=dur)],
                  human="quick", voice=[], stop=None, suspect=None)
        r = run(sc)
        probes += 1
        for v in r["violations"]:
            codes[v.split()[0]] += 1
        if r["violations"]:
            bad.append(dict(seed=sc["seed"], violations=r["violations"][:4], scenario=sc))
    summary = dict(scenarios=n, probes=probes, seed0=seed0, seconds=round(time.time() - t0), violations=sum(codes.values()), by_check=dict(codes),
                   outcomes=dict(outcomes), refusals=dict(refusals), asked_which_pipe_then_answered=ASKED["which_pipe"], mutant=os.environ.get("AUTO_FUZZ_MUTANT", ""), in_bound_scenarios=in_bound, in_bound_with_a_finding=found_correct,
                   out_of_bound_false_accusations=out_false, first_failures=bad[:5])
    json.dump(summary, open(out, "w"), indent=1)
    print(json.dumps(summary, indent=1)[:3000])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
