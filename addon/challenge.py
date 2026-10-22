"""Challenge mode - "Try to fool it".  DEV HOST ONLY, simulated house only.

A round hides a leak (or no leak, or one too small for the sensor) somewhere in the simulated house. The person then runs the
ordinary leak hunt from the ordinary screen and, while it runs, throws things at it:

    ambient_on / ambient_off   somebody uses water elsewhere in the house (an outside flow change; each click is ONE change)
    blackout_on / blackout_off the sensor's link drops (readings are lost, not delayed)
    hub_down / hub_up          the pipe controller cannot be reached
    jam:<pipe> / free          a pipe's valve sticks open
    reboot_sensor              the sensor restarts (new boot, counter back to zero)
    addon_down / addon_up      "pull the plug": the add-on stops running (no ticks, no renewals, no commands) while the world
                               and the simulated pipe controller's own watchdog carry on

`reveal()` then says what was hidden, what Leak Hunt said, whether that was right, and what a naive engine that believes the
first drop it sees (mutant M1) would have said about the very same readings.

Nothing here is reachable unless the server was started with LEAKHUNT_DEVHOST=1, and the routes answer only to the machine
they run on. The production tools (water_status, start_leak_hunt, ...) know nothing about it.
"""
from __future__ import annotations

import random
import threading
from typing import Dict, List, Optional, Tuple

from huntkit.core import Topology
from huntkit.mutants import SingleDropConfirms
from huntkit.sim import synth_history

from .actuators import OPEN
from .service import HuntService, LEASE_S, UserError

ACTIONS = ("ambient_on", "ambient_off", "blackout_on", "blackout_off", "hub_down", "hub_up", "jam", "free", "reboot_sensor",
           "addon_down", "addon_up")
AMBIENT_LPM = 1.5                    # an unmodelled tap: about the size of a small leak, which is what makes it confusing
HIST_BOOT = "hist-boot"
WORLD_CLAIMS = ("upstream_remainder", "no_flow_above_limit", "below_sensor_floor")   # outcomes that say where the water is NOT


# Scripted rounds: a hidden leak, a tap already running, and ONE outside change at a moment found by searching timings
# (tools/repro.py, scenario 2): 12 s after the 4th pipe is closed, i.e. in the middle of that pipe's measurement. A "first drop
# wins" engine credits the drop to the pipe that was just closed; Leak Hunt does not.
SCRIPTS = {
    "tap_stops": dict(
        title="A tap that was already running stops in the middle of a measurement",
        seed=11, before=["ambient_on"],
        events=[dict(after_close=4, delay_s=12.0, action="ambient_off")]),
}


class Challenge:
    def __init__(self, svc: HuntService, hid: str = "default"):
        self.svc, self.hid = svc, hid
        self._script: List[dict] = []
        self._script_title: Optional[str] = None
        svc.tick_hooks.append(self._due)
        self.round = 0
        self.seed: Optional[int] = None
        self.truth: Dict[str, float] = {}
        self.t_round: Optional[float] = None
        self.chaos: List[dict] = []
        self._flags = dict(ambient=False, blackout=False, hub_down=False, jammed=[])
        # "Pull the plug". The server's ticker calls svc.tick(); while the add-on is "down" that call is replaced by what still
        # happens without the add-on: the world's scripted events and the pipe controller's own watchdog. Nothing of the add-on
        # (polling, renewing, commanding, ending a hunt) runs. The wrapper exists only on a dev host that built a Challenge.
        self.addon_down = False
        self._t_down: Optional[float] = None
        self._lapse_mark = 0
        self._real_tick = svc.tick
        svc.tick = self._tick

    # ------------------------------------------------------------------ helpers
    def _parts(self):
        src, hub = self.svc._source(self.hid), self.svc._hub(self.hid)
        if not getattr(src, "simulated", False) or not hasattr(src, "blackout"):
            raise UserError("Challenge mode needs the simulated house.")
        if hub is None or not hasattr(hub, "stuck"):
            raise UserError("Challenge mode needs the simulated pipe controller (it is on by default in the demo).")
        return src, hub, self.svc._topology(self.hid)

    @staticmethod
    def _advance(src, now: float) -> None:
        src._sample_until(now)
        src.house.advance(max(0.0, now - src.house.t))

    def _draw(self, rng: random.Random, topo: Topology) -> Dict[str, float]:
        leaves = [i for i in topo.nodes if topo.is_leaf(i)]
        kind = rng.choices(["one", "two", "none", "tiny"], [5, 2, 1, 1])[0]
        if kind == "none":
            return {}
        if kind == "tiny":
            return {rng.choice(leaves): round(rng.uniform(0.3, 0.6), 2)}      # below what the sensor reads reliably
        n = 1 if kind == "one" else 2
        return {l: round(rng.uniform(1.4, 4.0), 2) for l in rng.sample(leaves, n)}

    # ------------------------------------------------------------------ rounds
    def new_round(self, seed: Optional[int] = None, script: Optional[str] = None) -> dict:
        svc = self.svc
        with svc._lock:
            hid, now = self.hid, svc.clock()
            self._parts()
            plan = None
            if script:
                plan = SCRIPTS.get(str(script))
                if plan is None:
                    raise UserError("Unknown scripted case.")
                seed = plan["seed"]
            h = svc.store.latest_hunt(hid)
            if h and h["ended"] is None:
                svc.stop_hunt(hid)
            seed = int(seed) if seed is not None else random.SystemRandom().randrange(1, 10 ** 6)
            topo = svc._topology(hid)
            truth = self._draw(random.Random(seed), topo)
            for l in svc.store.latches(hid):
                svc.store.clear_latch(hid, l["pipe"])
            svc.store.clear_offers(hid)
            svc.store.execute("DELETE FROM reading WHERE hid=?", (hid,))      # a new house: no sample ids or times can collide
            house = svc.household(hid)
            source = dict(house["source"])
            source["sources"] = dict(truth)
            svc.set_household(hid, house["name"], house["topology"], tz_min=house["tz_min"], source=source)   # fresh sensor + hub
            real = {l: q for l, q in truth.items() if q >= svc.cfg.floor_lpm}
            hist = synth_history(now, nights=14, leak_from_night=12 if real else None, leak_lpm=sum(real.values()) or 2.0,
                                 cfg=svc.cfg, tz_min=house["tz_min"], include_today=True)
            svc.store.add_readings(hid, hist)
            svc._poll(hid, now)
            self.round += 1
            self.seed, self.truth, self.t_round, self.chaos = seed, truth, now, []
            self._flags = dict(ambient=False, blackout=False, hub_down=False, jammed=[])
            self.addon_down, self._t_down, self._lapse_mark = False, None, 0
            self._script = [dict(e, fired=False) for e in plan["events"]] if plan else []
            self._script_title = plan["title"] if plan else None
            for a in (plan["before"] if plan else []):
                self.act(a)
            return self.state()

    def state(self) -> dict:
        return dict(round=self.round, seed=self.seed, started=self.t_round, hidden=True, flags=dict(self._flags),
                    chaos=[dict(c) for c in self.chaos], actions=list(ACTIONS), jammable=self._jammable(),
                    scripted=self._script_title, addon_down=self.addon_down)

    def _tick(self) -> None:
        if not self.addon_down:
            self._real_tick()
            return
        svc = self.svc
        with svc._lock:
            hub = svc._hubs.get(self.hid)
            if hub is not None and hasattr(hub, "expire"):
                hub.expire(svc.clock())            # the controller's own timer: it belongs to the controller, not to the add-on
            self._due()                            # the outside world keeps going

    def controller(self) -> dict:
        """What the simulated pipe controller reports, read from the controller itself: positions, the time its own timers have
        left, and every pipe it reopened because a lease ran out. Read-only; asking is the same as the controller's own check."""
        svc = self.svc
        with svc._lock:
            _, hub, topo = self._parts()
            now = svc.clock()
            reachable = True
            try:
                states = hub.pipes(now)
            except ConnectionError:
                states, reachable = {}, False
            outl = {o["id"]: o["label"] if any(w in o["label"] for w in ("tap", "hose")) else o["label"] + " tap"
                    for o in (svc.household(self.hid)["source"].get("outlets") or [])}
            name = lambda p: outl.get(p) or topo.label(p)
            rows = []
            for p in hub.pipe_ids():
                st = states.get(p)
                rows.append(dict(id=p, label=name(p), position=st.position if st else "unknown",
                                 rest=hub.rest(p) if hasattr(hub, "rest") else OPEN, outlet=p in outl,
                                 lease_left_s=None if st is None or st.lease_left_s is None else round(st.lease_left_s, 1)))
            lapsed = [dict(id=p, label=name(p), outlet=p in outl, after_s=round(until - self._t_down, 1))
                      for p, until in hub.lapsed[self._lapse_mark:] if self._t_down is not None and (p in topo.nodes or p in outl)]
            return dict(name="Pipe controller (simulated)", reachable=reachable, addon_down=self.addon_down, clock=now,
                        silent_s=None if self._t_down is None or not self.addon_down else round(now - self._t_down, 1),
                        time_scale=svc.time_scale, lease_s=LEASE_S, pipes=rows, lapsed_since_plug_pulled=lapsed)

    def _due(self) -> None:
        """Scripted events, anchored to what the hunt does (the k-th pipe it closes), not to the wall clock, so a slow person
        saying 'quiet' still meets the same unlucky moment."""
        if not self._script or self.t_round is None:
            return
        svc = self.svc
        hunt = svc.store.latest_hunt(self.hid)
        if not hunt or hunt["started"] < self.t_round or hunt["ended"] is not None:
            return
        closes = sorted(a["t"] for a in svc.store.actuations(self.hid, hunt["id"]) if a["ok"] and a["want"] == "closed" and a["source"] == "hunt")
        now = svc.clock()
        for e in self._script:
            if not e["fired"] and len(closes) >= e["after_close"] and now >= closes[e["after_close"] - 1] + e["delay_s"]:
                e["fired"] = True
                self.act(e["action"], e.get("arg"), by="script")

    def _jammable(self) -> List[dict]:
        try:
            topo = self.svc._topology(self.hid)
            hub = self.svc._hub(self.hid)
            return [dict(id=i, label=topo.label(i)) for i in topo.nodes if hub is not None and hub.has(i)]
        except Exception:
            return []

    # ------------------------------------------------------------------ chaos
    def act(self, action: str, arg: Optional[str] = None, by: str = "you") -> dict:
        svc = self.svc
        with svc._lock:
            if self.round == 0:
                raise UserError("Start a round first.")
            src, hub, topo = self._parts()
            now = svc.clock()
            if action not in ACTIONS:
                raise UserError("Unknown action.")
            if action in ("ambient_on", "ambient_off"):
                src.set_ambient(AMBIENT_LPM if action == "ambient_on" else 0.0, now)     # a delta: a running fill keeps its flow
                self._flags["ambient"] = action == "ambient_on"
            elif action in ("blackout_on", "blackout_off"):
                if action == "blackout_on":
                    svc._poll(self.hid, now)                             # what arrived before the link dropped is kept
                src.blackout = action == "blackout_on"
                self._flags["blackout"] = src.blackout
            elif action in ("hub_down", "hub_up"):
                hub.offline = action == "hub_down"
                self._flags["hub_down"] = hub.offline
            elif action == "jam":
                if not arg or not hub.has(arg):
                    raise UserError("Choose a pipe to jam.")
                hub.stuck[arg] = OPEN
                self._flags["jammed"] = sorted(hub.stuck)
            elif action == "free":
                hub.stuck.clear()
                self._flags["jammed"] = []
            elif action == "reboot_sensor":
                self._advance(src, now)
                src.house.reboot()
            elif action == "addon_down":
                if not self.addon_down:
                    self.addon_down, self._t_down, self._lapse_mark = True, now, len(getattr(hub, "lapsed", []))
            elif action == "addon_up":
                self.addon_down = False
            self.chaos.append(dict(t=now, action=action, arg=arg, by=by))
            return self.state()

    # ------------------------------------------------------------------ reveal
    @staticmethod
    def _has_leak(topo: Topology, node: str, leaks: set) -> bool:
        leaves = [node] if topo.is_leaf(node) else topo.subtree_leaves(node)
        return any(l in leaks for l in leaves)

    def _grade(self, topo: Topology, found: List[str], leaks: set) -> dict:
        wrong = [n for n in found if not self._has_leak(topo, n, leaks)]
        if wrong:
            return dict(grade="false_accusation", wrong=[topo.label(n) for n in wrong])
        if found:
            return dict(grade="right", wrong=[])
        return dict(grade="declined" if leaks else "nothing_to_find", wrong=[])

    @staticmethod
    def _found(view_claims) -> List[str]:
        return [c["node"] if isinstance(c, dict) else c.node for c in view_claims
                if (c["kind"] if isinstance(c, dict) else c.kind) == "source_confirmed"]

    def reveal(self) -> dict:
        """What was hidden, what Leak Hunt says about it, and what a 'first drop wins' engine says about the same readings.
        Works on a finished hunt (with its evidence hash) and on one still running (then it is 'as of now')."""
        svc = self.svc
        with svc._lock:
            if self.round == 0:
                raise UserError("Start a round first.")
            hid = self.hid
            hunt = svc.store.latest_hunt(hid)
            if not hunt or hunt["started"] < (self.t_round or 0.0):
                raise UserError("Run a leak hunt in this round first (use the screen above), then reveal.")
            topo = svc._topology(hid)
            status = svc.hunt_status(hid, hunt["id"])              # may end a hunt that has finished; harmless otherwise
            hunt = svc.store.hunt(hunt["id"])
            running = hunt["ended"] is None
            as_of = svc.clock() if running else hunt["ended"]
            floor = svc.cfg.floor_lpm
            real = {l for l, q in self.truth.items() if q >= floor}
            engine_found = self._found(svc._engine(hunt, as_of=as_of).view(as_of).claims)
            naive_found = self._found(svc._engine(hunt, as_of=as_of, cls=SingleDropConfirms).view(as_of).claims)
            eng, naive = self._grade(topo, engine_found, real), self._grade(topo, naive_found, real)
            end = as_of
            during = [c for c in self.chaos if hunt["started"] < c["t"] <= end]
            outside = sum(1 for c in during if c["action"] in ("ambient_on", "ambient_off"))
            faults = [c["action"] for c in during if c["action"] not in ("ambient_on", "ambient_off")]
            in_bound = outside <= 1
            lab = lambda ns: " and ".join(topo.label(n) for n in ns)
            now_ = "So far, " if running else ""
            if eng["grade"] == "false_accusation":
                if in_bound:
                    verdict = ("That named a valve with no leak behind it although at most one outside flow change happened. "
                               "This contradicts the project's own checks. Please report it with seed %d." % self.seed)
                else:
                    verdict = ("You fooled it: %d outside flow changes during one hunt is beyond the bound the project claims "
                               "(at most ONE; two well-timed ones can fool it, see docs/HONEST_UNKNOWNS.md)." % outside)
            elif eng["grade"] == "right":
                verdict = "%sRight. The flow was isolated to the water behind %s, and that is where the leak was." % (now_, lab(engine_found))
            elif eng["grade"] == "declined":
                tap = any(c["action"] == "ambient_on" for c in self.chaos)
                if not running and not tap and hunt["outcome"] in WORLD_CLAIMS:
                    verdict = ("It stopped with a statement about where the water is not (read it above), but a leak above the limit "
                               "was hidden behind %s and no outside tap was ever on. That contradicts the project's own checks. "
                               "Please report it with seed %d." % (lab(sorted(real)), self.seed))
                else:
                    verdict = ("%sIt has not named a source. A leak above the sensor's limit is hidden behind %s. Not naming it is a "
                               "miss or a wait, not a false accusation%s." % (
                                   now_, lab(sorted(real)),
                                   "; read its statement against the truth, because with %d outside changes a wrong answer is "
                                   "possible and documented" % outside if outside > 1 else ""))
            else:
                verdict = ("%sIt has named nothing, and nothing above the sensor's limit was hidden%s."
                           % (now_, " (a tiny leak was hidden below it)" if self.truth else ""))
            packet_sha = None
            if not running:
                packet_sha = svc.evidence(hid, hunt["id"])["sha256"]
            return dict(
                round=self.round, seed=self.seed, hunt_id=hunt["id"], running=running, hunt_outcome=hunt["outcome"],
                truth=[dict(node=l, label=topo.label(l), lpm=q, above_limit=q >= floor) for l, q in sorted(self.truth.items())],
                limit_lpm=floor, you_threw=[dict(action=c["action"], arg=c["arg"], by=c.get("by", "you"), after_s=round(c["t"] - hunt["started"], 1)) for c in during],
                outside_changes=outside, other_faults=faults, within_claimed_bound=in_bound,
                engine=dict(found=[topo.label(n) for n in engine_found], said=status.get("speech", ""), **eng),
                naive=dict(found=[topo.label(n) for n in naive_found], **naive,
                           same_as_engine=sorted(naive_found) == sorted(engine_found)),
                verdict=verdict, evidence_sha256=packet_sha)
