"""HuntService: everything the MCP tools do, with no MCP in it (so it can be tested with a fake clock).

Design rules
  * The engine is rebuilt from the store on every call. No session state: a hunt started in one Alexa+
    conversation can be continued in another, and a server restart loses nothing.
  * Only addon/actuators.py can move water, and only pipes that a configured Pipe Hub reports. It can never address
    the main safety valve. Control exists only when the OPERATOR configured a hub for the household; no tool
    can turn it on. See the safety policy in docs/PIPE_HUB.md and the constants below.
  * Every spoken line is the same string the screen shows (`speech` == `action.text`).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import asdict
from typing import Callable, Dict, List, Optional, Tuple

from huntkit import Config, HuntEngine, Topology
from huntkit.baseline import analyse
from huntkit.core import ROOT, Reading
from huntkit.engine import fmt_lpm
from huntkit.pipes import describe_pipes, join_or, name_pipes, resolve
from huntkit.sim import demo_topology, synth_history

from .actuators import CLOSED, OPEN, Ack, RestPipeHub, VirtualPipeHub
from .fill import Filler, FillError
from .modes import ModeError, Modes
from .sources import VirtualSource
from .store import Store

ENGINE_VERSION = "huntkit-0.2"
ACTIVE_TTL_S = 30 * 60.0          # a hunt untouched for 30 minutes is abandoned, not resumed
DEDUPE_S = 3.0                    # an identical report inside this window is a retry, not a new step
PRIOR_BOOST = 3.0                 # "I think it's the toilet" triples that leaf's prior for this hunt

# ---- safety policy for pipe control (each line is enforced in code and exercised by tests/test_auto.py / tools/auto_fuzz.py)
LEASE_S = 120.0                   # every close made during a hunt is a lease: the HUB reopens the pipe when it lapses
LEASE_RENEW_FRAC = 0.5            # renewed while the hunt is healthy once less than this share is left
STALE_ABORT_S = 60.0              # no telemetry for this long: stop, reopen everything
HUB_GRACE_S = 15.0                # the hub may be unreachable this long before the hunt is abandoned
MAX_COMMANDS = 60                 # state-changing valve commands one hunt may issue
MAX_AUTO_RECHECKS = 6             # automatic "re-measure" steps before we give up and say the house isn't quiet
REMEASURE_WHY = ("unstable_flow", "counter_or_clock_inconsistent")   # an unclean window never turns clean by waiting: start a new one
MIN_PIPE_INTERVAL_S = 2.0         # one pipe is never flipped twice inside this window (valve and relay wear)
MAX_AT_ONCE = 2                   # a voice command that moves more pipes than this needs an explicit yes
OFFER_S = 120.0                   # a confirmed culprit is held closed on a lease this long while the customer decides

VALID_KINDS = ("quiet", "close", "open", "undo")

CONSENT_TEXT = ("I can run this test myself. I'll close your pipes one after another until the flow stops, so water to them "
                "is off for a few minutes in total while I measure. I'll turn everything back on when I'm done, or the "
                "moment you say stop. If I find the leak, I'll ask whether you want that one kept off; if you don't say yes, "
                "it comes back on too.")
CHOOSE_TEXT = ("I can find it two ways. I can run the test myself: I'll close your pipes one after another, so water to "
               "them is off for a few minutes in total, and I'll turn everything back on when I'm done or when you say "
               "stop; if I find the leak I'll ask before keeping anything off. Or I can guide you valve by valve. "
               "Which would you like?")
PAUSE_TEXT = ("I've paused: my water-sensor data is out of date. I won't switch any pipe until it is back. If it stays "
              "out, I'll stop and turn everything back on.")
AUTO_INTRO = ("Before I start, make sure nothing is using water: taps, washing machine, cistern fills. I've opened all "
              "my pipes and I'll run the rest myself. Tell me when it's quiet.")

ABORT_TEXT = {
    "telemetry_stale": "I lost sight of your water sensor, so I stopped the test and reopened everything I closed.",
    "pipe_fault": "{pipe} didn't move when I asked, so I stopped the test and reopened everything I could.",
    "pipe_moved": "{pipe} isn't where I left it, so something else moved it. I stopped rather than guess, and reopened what I closed.",
    "pipe_missing": "{pipe} disappeared from the pipe controller, so I stopped the test and reopened what I could.",
    "pipe_too_slow": "{pipe} took too long to confirm its position for a clean measurement, so I stopped and reopened everything.",
    "hub_unreachable": "I can't reach your pipe controller. I stopped the test; any pipe I closed reopens by itself within two minutes.",
    "lease_renew_failed": "I couldn't keep {pipe} closed reliably, so I stopped and reopened everything.",
    "command_cap": "This test needed more valve moves than I allow myself, so I stopped and reopened everything.",
    "house_not_quiet": "The water use in the house keeps changing between measurements, so I can't give you a trustworthy answer. I stopped and put everything back.",
    "no_hub": "I can no longer reach the pipe controller, so I stopped.",
}

# When the controller has not confirmed every pipe back on, a sentence that says "I put everything back" would be untrue: it is
# reworded to what was done (the command was sent), and the speech then names each unconfirmed pipe and when it reopens itself.
RESTORE_CLAIMS = [
    ("I've put back every pipe I closed.", "I've told your pipe controller to put back every pipe I closed."),
    ("reopened everything I closed", "told your pipe controller to reopen everything I closed"),
    ("reopened everything I could", "told your pipe controller to reopen everything I could"),
    ("reopened what I closed", "told your pipe controller to reopen what I closed"),
    ("reopened what I could", "told your pipe controller to reopen what I could"),
    ("reopened everything", "told your pipe controller to reopen everything"),
    ("put everything back", "told your pipe controller to put everything back"),
    ("and put every pipe back.", "and told your pipe controller to put every pipe back."),
]

# Simulated taps the demo controller can fill from. Each has the installer's nominal rate and a slightly different "true" one.
DEMO_TUB = dict(id="tub", label="bathtub", parent="bath", aliases=["bath", "tub", "bathtub", "the bath"], max_l=150.0, preset_l=120.0,
                nominal_lpm=12.0, sim_lpm=11.4, vessel=True)
DEMO_OUTLETS = [
    DEMO_TUB,
    dict(id="ktap", label="kitchen tap", parent="kitchen1", aliases=["kitchen", "sink", "kitchen sink", "pot", "the pot"], max_l=20.0,
         preset_l=5.0, nominal_lpm=8.0, sim_lpm=7.6),
    dict(id="hose", label="garden hose", parent="garden", aliases=["hose", "plants", "the plants", "garden"], max_l=100.0,
         preset_l=30.0, nominal_lpm=15.0, sim_lpm=14.2),
]

ASSUMPTIONS = [
    "The flow sensor reports cumulative litres; flows are derived from differences of those totals, so the "
    "10-second telemetry cadence does not lose volume.",
    "Only continuous flow at or above the sensor floor can be located. Drips below it are out of scope.",
    "In a guided hunt the engine learns what a valve did only from the flow sensor. It cannot see which valve a person "
    "actually turned; a wrong valve would show up as 'no change', never as a finding.",
    "In an automatic hunt the pipe positions come from the Pipe Hub's own position sensing and are assumed truthful. "
    "A pipe that reports closed while still passing water would look like a pipe that is not leaking.",
    "A source that switches on and off by itself (a refilling cistern) can look like a valve effect; "
    "the engine tests window stability and requires a close-open-close replication before it says 'confirmed'.",
    "Thresholds (floor, noise, variability) are engineering assumptions until measured on the real rig.",
]


class UserError(Exception):
    """An error whose message is safe and useful to say out loud."""


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class HuntService:
    utility = "water"                 # addon/power.py subclasses this for electricity (smart plugs + an energy meter)
    hunt_prefix = "h_"
    hunt_skips_protected = False      # power: plugs that must stay on are not part of the hunt at all
    assumptions = ASSUMPTIONS

    def __init__(self, store: Store, clock: Callable[[], float], cfg: Config = Config(), *, key: bytes = b"dev-key",
                 public_base_url: str = "", time_scale: float = 1.0):
        self.store, self.clock, self.cfg, self.key = store, clock, cfg, key
        self.time_scale = time_scale          # >1 only for a labelled demo time-lapse
        self.public_base_url = public_base_url.rstrip("/")
        self._sources: Dict[str, object] = {}
        self._hubs: Dict[str, object] = {}
        self._hub_down_since: Dict[str, float] = {}
        self._last_poll: Dict[str, float] = {}
        self._lock = threading.RLock()
        self.tick_hooks: List[Callable[[], None]] = []        # dev-host extras only (challenge mode); empty in production
        self.filler = Filler(self)                              # "fill the bath, 120 litres" (addon/fill.py)
        self.modes = Modes(self)                                # "sleep mode", "away mode", "I'm home" (addon/modes.py)

    # ================================================================ households
    def set_household(self, hid: str, name: str, topology: List[dict], *, tz_min: int = 330,
                      source: Optional[dict] = None) -> dict:
        with self._lock:
            Topology.from_json(topology)                        # validates the tree
            existing = self.store.household(hid)
            src = source if source is not None else (existing["source"] if existing else {"kind": "virtual", "sources": {}})
            self.store.put_household(hid, name, topology, tz_min, src, self.clock())
            self._sources.pop(hid, None)
            self._hubs.pop(hid, None)
            return self.household(hid)

    def configure_pipehub(self, hid: str, cfg: Optional[dict]) -> dict:
        """OPERATOR API (env / CLI only; there is deliberately no tool for this): attach or detach a pipe hub."""
        with self._lock:
            h = self.household(hid)
            src = dict(h["source"])
            if cfg:
                src["pipehub"] = dict(cfg)
            else:
                src.pop("pipehub", None)
            return self.set_household(hid, h["name"], h["topology"], tz_min=h["tz_min"], source=src)

    def ensure_demo_household(self, hid: str = "default", *, sources: Optional[Dict[str, float]] = None,
                              nights: int = 14, leak_nights: int = 3, leak_lpm: float = 2.0, tz_min: int = 330,
                              control: bool = True, manual: Optional[List[str]] = None, outlets: bool = False) -> dict:
        """A SIMULATED house: a virtual plumbing rig plus synthetic overnight history. Always labelled as such.

        `control=True` also gives it a simulated Pipe Hub (every pipe except those listed in `manual`). `outlets=True` adds a
        simulated bathtub tap valve on the bathroom branch that the hub can fill from (rests closed, opens only on a lease)."""
        with self._lock:
            if self.store.household(hid):
                return self.household(hid)
            sources = {"toilet": 2.0} if sources is None else sources
            topo = demo_topology()
            src = {"kind": "virtual", "sources": sources}
            if outlets:
                src["outlets"] = [dict(o) for o in DEMO_OUTLETS]
            if control:
                src["pipehub"] = {"kind": "virtual", "enabled": True,
                                  "pipes": [i for i in topo.nodes if i not in (manual or [])]}
                if outlets:
                    src["pipehub"]["outlets"] = [o["id"] for o in DEMO_OUTLETS]
            self.set_household(hid, "Demo house (simulated)", topo.to_json(), tz_min=tz_min, source=src)
            now = self.clock()
            total_nights = nights + 1
            hist = synth_history(now, nights=nights, leak_from_night=(total_nights - leak_nights) if leak_nights and sources else None,
                                 leak_lpm=leak_lpm, cfg=self.cfg, tz_min=tz_min, include_today=True)
            self.store.add_readings(hid, hist)
            return self.household(hid)

    def household(self, hid: str) -> dict:
        h = self.store.household(hid)
        if not h:
            raise UserError("No household is set up yet.")
        return h

    def _topology(self, hid: str) -> Topology:
        return Topology.from_json(self.household(hid)["topology"])

    def _source(self, hid: str):
        if hid in self._sources:
            return self._sources[hid]
        h = self.household(hid)
        s = h["source"]
        kind = s.get("kind")
        if kind == "virtual":
            src = VirtualSource(Topology.from_json(h["topology"]), dict(s.get("sources", {})), self.cfg, self.clock)
            for o in s.get("outlets") or []:
                src.add_outlet(o["id"], float(o.get("sim_lpm") or o.get("nominal_lpm") or 12.0), o.get("parent"))
        elif kind == "sensor":
            try:
                from .sensor_adapter import RestSensorSource           # optional file: not part of this simulation-only build
            except ImportError:
                raise UserError("This build has no real-sensor adapter; it runs on the simulated house only.")
            token = s.get("token") or os.environ.get(s.get("token_env", "LEAKHUNT_SENSOR_TOKEN"), "")
            src = RestSensorSource(s["base_url"], s["device_id"], token)
        else:
            raise UserError("This household has no sensor source configured.")
        self._sources[hid] = src
        return src

    def _poll(self, hid: str, now: float, min_gap: float = 0.0) -> Optional[str]:
        """Pull new readings into the store. Returns an error tag (never raises)."""
        if min_gap and now - self._last_poll.get(hid, -1e18) < min_gap:
            return None
        self._last_poll[hid] = now
        try:
            rs = self._source(hid).poll(now)
            self.store.add_readings(hid, rs)
            return None
        except UserError:
            raise
        except Exception as e:                                   # network, auth, parse: degrade to stored data
            return type(e).__name__

    def _flush(self, hid: str, now: float) -> None:
        """A simulated valve move produces telemetry immediately; a real one shows up in the next real poll."""
        src = self._source(hid)
        if getattr(src, "simulated", False):
            self.store.add_readings(hid, src.poll(now))

    # ================================================================ pipes (voice control of the Pipe Hub)
    def _hub(self, hid: str):
        """The household's pipe hub, or None. Exists only if the operator configured and enabled one."""
        if hid in self._hubs:
            return self._hubs[hid]
        h = self.store.household(hid)
        hub = None
        cfg = ((h or {}).get("source") or {}).get("pipehub")
        if cfg and cfg.get("enabled") is True:
            kind = cfg.get("kind")
            if kind == "virtual" and h["source"].get("kind") == "virtual":
                ids = cfg.get("pipes") or list(Topology.from_json(h["topology"]).nodes)
                outs = [o["id"] for o in (h["source"].get("outlets") or []) if o["id"] in (cfg.get("outlets") or [])]
                hub = VirtualPipeHub(self._source(hid), ids, outlets=outs)
            elif kind == "rest":
                token = cfg.get("token") or os.environ.get(cfg.get("token_env", "LEAKHUNT_HUB_TOKEN"), "")
                hub = RestPipeHub(cfg["base_url"], token, self.clock, allow_insecure_lan=bool(cfg.get("allow_insecure_lan")))
        self._hubs[hid] = hub
        return hub

    def control_available(self, hid: str) -> bool:
        return self._hub(hid) is not None

    def _actuated(self, hid: str, topo: Topology) -> List[str]:
        hub = self._hub(hid)
        if hub is None:
            return []
        return [i for i, n in topo.nodes.items() if hub.has(i) and not n.protect]

    def _command(self, hid: str, hunt_id: Optional[str], pipe: str, want: str, source: str,
                 lease_s: Optional[float] = None, *, force: bool = False, audit: bool = True) -> Ack:
        """The ONLY place a valve command is sent. Rate-limited, audited, never raises."""
        hub = self._hub(hid)
        t = self.clock()
        if hub is None:
            return Ack(False, pipe, want, "unknown", t, t, "no_hub")
        last = self.store.last_actuation_t(hid, pipe)
        if not force and last is not None and 0 <= t - last < MIN_PIPE_INTERVAL_S:
            if audit:
                self.store.add_actuation(hid, hunt_id, t, pipe, want, source, False, "rate_limited")
            return Ack(False, pipe, want, "unknown", t, t, "rate_limited")
        try:
            ack = hub.command(pipe, want, t, lease_s)
        except Exception as e:                                       # a hub that raises is a hub that failed
            ack = Ack(False, pipe, want, "unknown", t, self.clock(), type(e).__name__)
        if audit:
            self.store.add_actuation(hid, hunt_id, ack.t_ack, pipe, want, source, ack.ok,
                                     ack.error if not ack.ok else (f"lease {int(lease_s)}s" if lease_s else ""))
        self._flush(hid, self.clock())
        return ack

    def _positions(self, hid: str, now: float) -> Dict[str, object]:
        hub = self._hub(hid)
        if hub is None:
            return {}
        pos = hub.pipes(now)
        self._flush(hid, now)                                            # a lapsed lease may have moved a simulated valve
        return pos

    def _pipe_rows(self, hid: str, topo: Topology, pos: Optional[dict] = None) -> List[dict]:
        acts = set(self._actuated(hid, topo)) if self._hub(hid) else set()
        latched = {l["pipe"]: l for l in self.store.latches(hid)}
        offered = {o["pipe"]: o for o in self.store.offers(hid, self.clock())}
        rows = []
        for n in topo.nodes.values():
            st = (pos or {}).get(n.id)
            rows.append(dict(id=n.id, label=n.label, room=n.room, number=n.number, aliases=list(n.aliases), how=n.how,
                             parent=n.parent, protect=n.protect, actuated=n.id in acts,
                             position=None if st is None else st.position,
                             lease_left_s=None if st is None or st.lease_left_s is None else round(st.lease_left_s, 1),
                             latched=n.id in latched, offered=n.id in offered,
                             offer_left_s=None if n.id not in offered else round(max(0.0, offered[n.id]["expires"] - self.clock()), 1)))
        return rows

    def list_pipes(self, hid: str) -> dict:
        with self._lock:
            now = self.clock()
            topo = self._topology(hid)
            hub = self._hub(hid)
            pos, err = {}, None
            if hub is not None:
                try:
                    pos = hub.pipes(now)
                except Exception as e:
                    err = type(e).__name__
            rows = self._pipe_rows(hid, topo, pos)
            act = [r for r in rows if r["actuated"]]
            manual = [r for r in rows if not r["actuated"] and not r["protect"]]
            guarded = [r for r in rows if r["protect"]]
            if hub is None:
                speech = ("I can't switch pipes in this home, but I can still find a leak by guiding you. "
                          "Your valves are: " + describe_pipes(topo))
            elif err:
                speech = "I can't reach your pipe controller right now, so I don't know what state the pipes are in."
            else:
                off = [r["label"] for r in act if r["position"] == CLOSED]
                speech = f"I can switch {len(act)} pipe{'s' if len(act) != 1 else ''}: {join_or([r['label'] for r in act], 'and')}."
                speech += (f" Right now {join_or(off, 'and')} {'is' if len(off) == 1 else 'are'} off." if off
                           else " All of them are on.")
                if manual:
                    speech += (f" {join_or([r['label'] for r in manual], 'and')} "
                               f"{'is a hand valve' if len(manual) == 1 else 'are hand valves'}, so I can't switch "
                               f"{'it' if len(manual) == 1 else 'those'}.")
                if guarded:
                    speech += (f" I never switch {join_or(['the ' + r['label'] for r in guarded], 'or')}: "
                               f"{'it feeds' if len(guarded) == 1 else 'they feed'} something that must stay on.")
            lat = self.store.latches(hid)
            if lat:
                names = join_or([topo.label(l["pipe"]) for l in lat if l["pipe"] in topo.nodes], "and")
                speech += f" I'm keeping {names} closed because you asked me to after I found a leak there."
            offs = [o for o in self.store.offers(hid, now) if o["pipe"] in topo.nodes]
            if offs:
                speech += (f" I'm holding {join_or([topo.label(o['pipe']) for o in offs], 'and')} off while you decide whether to "
                           f"keep {'it' if len(offs) == 1 else 'them'} off until {'it has' if len(offs) == 1 else 'they have'} been checked.")
            return dict(kind="pipes", speech=speech, simulated=bool(self._source(hid).simulated), control_available=hub is not None,
                        pipes=rows, latched=[l["pipe"] for l in lat], offered=[o["pipe"] for o in offs], hub_error=err,
                        hunt_active=bool(self._active_hunt(hid, now)), time_scale=self.time_scale)

    def set_pipe(self, hid: str, pipe: str, state: str, *, all_in_room: bool = False, confirm: bool = False) -> dict:
        """'Alexa, turn on the kitchen pipe.' Resolves the name, asks when it is ambiguous, applies the safety policy."""
        with self._lock:
            now = self.clock()
            hub = self._hub(hid)
            if hub is None:
                raise UserError("I can't switch pipes in this home. I can still guide you through a leak hunt.")
            if state not in ("on", "off"):
                raise UserError("Do you want that pipe on or off?")
            topo = self._topology(hid)
            retry = dict(pipe=pipe, state=state, all_in_room=all_in_room)      # what a screen sends back, plus confirm, to say yes
            m = resolve(topo, pipe)
            if m.kind == "none":
                raise UserError(m.question)
            if m.kind == "many" and not (m.all_requested or all_in_room):
                p = self.list_pipes(hid)
                return dict(p, kind="choose_pipe", speech=m.question, wanted=state, retry=retry,
                            options=[dict(id=i, label=topo.label(i)) for i in m.nodes])
            ids = list(m.nodes)
            if m.fuzzy and not confirm:                    # a mis-heard word was repaired: never act on a repaired guess unasked
                p = self.list_pipes(hid)
                return dict(p, kind="needs_confirm", reason="heard", wanted=state, pipe=pipe, retry=retry,
                            speech=f"Did you mean {join_or([topo.label(i) for i in ids], 'and')}? Say yes and I'll turn it {state}.")
            protected = [i for i in ids if topo.nodes[i].protect]
            acts = set(self._actuated(hid, topo))
            hand = [i for i in ids if i not in acts and i not in protected]
            todo = [i for i in ids if i in acts]
            if not todo:
                if protected:
                    raise UserError(f"{join_or([topo.label(i) for i in protected], 'and')} feeds something safety-critical, so I never switch it.")
                where = "; ".join(f"{topo.label(i)}" + (f" is {topo.nodes[i].how}" if topo.nodes[i].how else "") for i in hand)
                raise UserError(f"{where}. That is a hand valve, so I can't switch it for you.")
            want = OPEN if state == "on" else CLOSED
            active = self._active_hunt(hid, now)
            if active and not confirm:
                p = self.list_pipes(hid)
                return dict(p, kind="needs_confirm", reason="hunt_active", wanted=state, pipe=pipe, retry=retry,
                            speech="A leak hunt is running. Switching a pipe ends it, and I'll put back everything I closed. "
                                   "Do you want me to stop the hunt and switch it?")
            if len(todo) > MAX_AT_ONCE and not confirm:
                p = self.list_pipes(hid)
                return dict(p, kind="needs_confirm", reason="many_pipes", wanted=state, pipe=pipe, retry=retry,
                            speech=f"That is {len(todo)} pipes: {join_or([topo.label(i) for i in todo], 'and')}. Do you want me to turn them all {state}?")
            lat = {l["pipe"]: l for l in self.store.latches(hid)}
            offers = {o["pipe"]: o for o in self.store.offers(hid, now)}      # open "keep it closed?" questions
            if want == OPEN:
                held = [i for i in todo if i in lat]
                if held and not confirm:
                    p = self.list_pipes(hid)
                    return dict(p, kind="needs_confirm", reason="latched", wanted=state, pipe=pipe, retry=retry,
                                speech=f"{join_or([topo.label(i) for i in held], 'and')} {'is' if len(held) == 1 else 'are'} closed because I found a "
                                       f"leak there. Has it been repaired, and do you want it back on?")
                last = self._last_reading(hid)
                if last is not None and (last.valve != "OPEN" or last.leak):
                    raise UserError("Your main safety valve is closed because it detected a leak. I won't open pipes "
                                    "until you've inspected the plumbing and re-armed it at the device.")
            if active:                                              # confirmed: the customer's switch wins over the hunt
                self._end_hunt(self.store.hunt(active), now, "stopped")
            results: List[Tuple[str, Ack]] = []
            kept: List[str] = []
            for i in todo:
                ack = self._command(hid, None, i, want, "voice", None)
                results.append((i, ack))
                if ack.ok and want == OPEN and i in lat:
                    self.store.clear_latch(hid, i)
                if ack.ok and i in offers:                           # the customer is answering the isolation question
                    if want == CLOSED:                               # an explicit yes: the only way a pipe becomes latched
                        self.store.set_latch(hid, i, now, offers[i]["hunt_id"], offers[i]["lpm"])
                        kept.append(i)
                    self.store.clear_offer(hid, i)
            ok = [i for i, a in results if a.ok]
            bad = [(i, a) for i, a in results if not a.ok]
            if not ok:
                why = bad[0][1].error
                if why == "rate_limited":
                    raise UserError("That pipe was only just switched. Give it a couple of seconds.")
                raise UserError(f"{topo.label(bad[0][0])} didn't respond ({why}), so it hasn't changed.")
            pos = {}
            try:
                pos = self._positions(hid, self.clock())
            except Exception:
                pass
            said = join_or([topo.label(i) for i in ok], "and")
            speech = f"{said[:1].upper() + said[1:]} {'is' if len(ok) == 1 else 'are'} {state}."
            if kept:
                speech += (f" I'll keep {join_or(['the ' + topo.label(i) for i in kept], 'and')} off until you tell me it's been repaired. "
                           f"Then say 'turn on the {topo.label(kept[0])}'.")
            if bad:
                speech += f" {join_or([topo.label(i) for i, _ in bad], 'and')} didn't respond."
            if hand:
                speech += f" {join_or([topo.label(i) for i in hand], 'and')} {'is a hand valve' if len(hand) == 1 else 'are hand valves'}, so I left {'it' if len(hand) == 1 else 'them'}."
            rows = self._pipe_rows(hid, topo, pos)
            return dict(kind="pipes", speech=speech, simulated=bool(self._source(hid).simulated), control_available=True,
                        pipes=rows, changed=ok, kept=kept, latched=[l["pipe"] for l in self.store.latches(hid)], hub_error=None,
                        hunt_active=False, time_scale=self.time_scale)

    def _last_reading(self, hid: str) -> Optional[Reading]:
        rs = self.store.readings(hid, since=self.clock() - 600)
        return rs[-1] if rs else None

    # ================================================================ filling ("fill the bath, 120 litres"; addon/fill.py)
    def start_fill(self, hid: str, outlet: str = "", litres: Optional[float] = None) -> dict:
        try:
            return self.filler.start(hid, outlet, litres)
        except FillError as e:
            raise UserError(str(e))

    def stop_fill(self, hid: str) -> dict:
        try:
            return self.filler.stop(hid)
        except FillError as e:
            raise UserError(str(e))

    def fill_status(self, hid: str, fill_id: Optional[str] = None) -> dict:
        try:
            return self.filler.status(hid, fill_id)
        except FillError as e:
            raise UserError(str(e))

    # ================================================================ house modes ("sleep mode", "away mode"; addon/modes.py)
    def house_mode(self, hid: str, mode: str = "", confirm: bool = False) -> dict:
        try:
            return self.modes.set(hid, mode, confirm)
        except ModeError as e:
            raise UserError(str(e))

    # ================================================================ read-only status
    def water_status(self, hid: str) -> dict:
        with self._lock:
            now = self.clock()
            err = self._poll(hid, now)
            h = self.household(hid)
            src = self._source(hid)
            last = self.store.readings(hid, since=now - 600)
            age = (now - last[-1].t) if last else None
            stale = age is None or age > self.cfg.stale_s
            flow = None
            if not stale and len(last) >= 2:
                a, b = last[-2], last[-1]
                if a.boot_id == b.boot_id and b.uptime_ms > a.uptime_ms and b.out_total_l >= a.out_total_l:
                    flow = (b.out_total_l - a.out_total_l) / ((b.uptime_ms - a.uptime_ms) / 60000.0)
            night = self._night(hid, now)
            active = self._active_hunt(hid, now)
            hunt_hint = night["verdict"] in ("new_overnight_flow", "ongoing_overnight_flow")
            lines = [night["text"]]
            if stale:
                lines.append("I can't see live data from your water sensor right now.")
            elif flow is None:
                lines.append("I don't have a clean flow reading yet.")
            elif flow < self.cfg.floor_lpm:
                lines.append(f"Right now nothing is flowing above what your sensor can read reliably "
                             f"({fmt_lpm(self.cfg.floor_lpm)}).")
            else:
                lines.append(f"Right now {fmt_lpm(flow)} is flowing through your main line.")
            topo = self._topology(hid)
            lat = [l for l in self.store.latches(hid) if l["pipe"] in topo.nodes]
            if lat:
                lines.append(f"I'm still keeping {join_or([topo.label(l['pipe']) for l in lat], 'and')} closed because of the leak I found there.")
            if active:
                lines.append("You have a leak hunt in progress; I can pick it up where we left off.")
            elif hunt_hint:
                lines.append("Want me to find it?" if self.control_available(hid) else
                             "Want me to walk you through finding it, one valve at a time?")
            speech = " ".join(lines)
            return dict(kind="status", simulated=src.simulated, time_scale=self.time_scale, floor_lpm=self.cfg.floor_lpm,
                        household=h["name"], speech=speech,
                        flow_now_lpm=None if flow is None else round(flow, 3), data_age_s=None if age is None else round(age, 1),
                        stale=stale, night=night, active_hunt=active, suggest_hunt=bool(hunt_hint and not active),
                        source_error=err, control_available=self.control_available(hid),
                        latched=[l["pipe"] for l in lat])

    def _night(self, hid: str, now: float) -> dict:
        h = self.household(hid)
        rs = self.store.readings(hid, since=now - 15 * 86400)
        rep = analyse(rs, self.cfg, tz_min=h["tz_min"])
        return dict(verdict=rep.verdict, text=rep.text, baseline_median_lpm=rep.baseline_median_lpm,
                    nights=[dict(date=n.date, min_lpm=None if n.min_lpm is None else round(n.min_lpm, 3),
                                 coverage=round(n.coverage, 2)) for n in rep.nights[-14:]])

    def night_flow(self, hid: str) -> dict:
        with self._lock:
            now = self.clock()
            self._poll(hid, now)
            n = self._night(hid, now)
            return dict(kind="night_flow", simulated=self._source(hid).simulated, time_scale=self.time_scale,
                        floor_lpm=self.cfg.floor_lpm, speech=n["text"], **n)

    # ================================================================ hunts
    def _active_hunt(self, hid: str, now: float) -> Optional[str]:
        h = self.store.latest_hunt(hid)
        if not h or h["ended"] is not None:
            return None
        reps = self.store.reports(h["id"])
        last = max([h["started"]] + [r["t"] for r in reps])
        if now - last > ACTIVE_TTL_S:
            self._end_hunt(h, now, "abandoned")
            return None
        return h["id"]

    def _resolve_nodes(self, topo: Topology, names: List[str], *, many_ok: bool = False) -> List[str]:
        out: List[str] = []
        for raw in names or []:
            if raw in topo.nodes:                                         # an id (the UI and the tools hand these back) is exact
                out.append(raw)
                continue
            m = resolve(topo, raw)
            if m.kind == "none":
                raise UserError(m.question)
            if m.kind == "many" and not (many_ok and m.all_requested):
                raise UserError(m.question if m.question else f"'{raw}' could mean " + " or ".join(topo.label(i) for i in m.nodes) + ". Which one?")
            out.extend(m.nodes)
        return out

    def start_hunt(self, hid: str, *, suspect: Optional[List[str]] = None, resume: bool = True,
                   mode: Optional[str] = None, consent: bool = False) -> dict:
        with self._lock:
            now = self.clock()
            self._poll(hid, now)
            filling = self.filler.active(hid)
            if filling:
                raise UserError(f"I'm filling the {filling['state']['label']} right now, and a leak hunt needs the house quiet. "
                                f"I'll look for the leak when it's done, or say 'stop filling' first.")
            hm = self.store.house_mode(hid)
            if hm:
                back = {"sleep": "I'm up", "away": "I'm home"}.get(hm["mode"], "I'm home")
                raise UserError(f"{hm['mode'].capitalize()} mode is on, so most of your pipes are off and a hunt can't measure them. "
                                f"Say '{back}' first, then ask me to find the leak.")
            if mode not in (None, "auto", "guided"):
                raise UserError("I can run the test myself or guide you. Which would you like?")
            if resume:
                active = self._active_hunt(hid, now)
                if active:
                    out = self._present(active, now)
                    out["resumed"] = True
                    return out
            topo = self._topology(hid)
            avail = self.control_available(hid)
            note = ""
            if mode is None and avail:
                return dict(kind="choose_mode", speech=CHOOSE_TEXT, consent_text=CONSENT_TEXT, simulated=bool(self._source(hid).simulated),
                            time_scale=self.time_scale, options=["auto", "guided"], control_available=True)
            if mode == "auto" and not avail:
                mode, note = "guided", "I can't switch pipes in this home, so I'll guide you instead. "
            mode = mode or "guided"
            if mode == "auto":
                if not consent:
                    return dict(kind="needs_consent", speech=CONSENT_TEXT, consent_text=CONSENT_TEXT,
                                simulated=bool(self._source(hid).simulated), time_scale=self.time_scale, control_available=True)
                self._auto_preflight(hid, topo, now)
            nodes = [r for r in topo.to_json() if not (self.hunt_skips_protected and r.get("protect") and topo.is_leaf(r["id"]))]
            boosted = set()
            for sid in self._resolve_nodes(topo, suspect or [], many_ok=True):
                boosted.update(topo.subtree_leaves(sid) if not topo.is_leaf(sid) else [sid])
            for row in nodes:
                if row["id"] in boosted:
                    row["prior"] = float(row["prior"]) * PRIOR_BOOST
            src = self._source(hid)
            hunt_id = self.hunt_prefix + uuid.uuid4().hex[:8]
            if mode == "auto":
                self.store.clear_offers(hid)                             # a new test supersedes an open "keep it closed?" question
                self._auto_open_all(hid, topo, now)                      # baseline needs every pipe open
                self._reset_hand_valves(hid, topo, now)
            elif hasattr(src, "reset_valves"):                           # simulated house only: new hunt = everything re-opened
                self.store.add_readings(hid, src.reset_valves(now))
            self.store.create_hunt(hunt_id, hid, now, nodes, mode=mode, consent_t=now if mode == "auto" else None)
            out = self._present(hunt_id, now)
            out["resumed"] = False
            if note:
                out["speech"] = note + out["speech"]
            return out

    def _auto_preflight(self, hid: str, topo: Topology, now: float) -> None:
        """Everything that must be true before Alexa moves a single valve."""
        lat = [l for l in self.store.latches(hid) if l["pipe"] in topo.nodes]
        if lat:
            names = join_or([topo.label(l["pipe"]) for l in lat], "and")
            raise UserError(f"{names} {'is' if len(lat) == 1 else 'are'} still closed because of the leak I found. "
                            f"Once it's repaired, say 'turn on {topo.label(lat[0]['pipe'])}', then I can look again.")
        last = self._last_reading(hid)
        if last is None or now - last.t > self.cfg.stale_s:
            raise UserError("I can't see live data from your water sensor, so I won't start moving pipes. Try again in a minute.")
        if last.valve != "OPEN" or last.leak:
            raise UserError("Your main safety valve is closed because it detected a leak. I can't reopen it and I won't touch "
                            "your pipes until you've inspected the plumbing and re-armed it at the device.")
        try:
            self._positions(hid, now)
        except Exception:
            raise UserError("I can't reach your pipe controller, so I can't run the test myself. I can guide you instead.")

    def _auto_open_all(self, hid: str, topo: Topology, now: float) -> None:
        pos = {}
        try:
            pos = self._positions(hid, now)
        except Exception:
            pass
        for p in self._actuated(hid, topo):
            if p not in pos or pos[p].position != OPEN:
                self._command(hid, None, p, OPEN, "hunt-start", None, force=True)

    def _reset_hand_valves(self, hid: str, topo: Topology, now: float) -> None:
        """Simulated house only: hand valves are reset between demo hunts, like reset_valves does for a guided one."""
        src = self._source(hid)
        if getattr(src, "simulated", False):
            acts = set(self._actuated(hid, topo))
            for n in list(src.physical_closed()):
                if n not in acts:
                    src.set_closed(n, False, now)
            self._flush(hid, now)

    def report(self, hid: str, hunt_id: Optional[str], kind: str, node: Optional[str] = None) -> dict:
        with self._lock:
            now = self.clock()
            hunt_id = hunt_id or self._active_hunt(hid, now) or ""
            hunt = self.store.hunt(hunt_id) if hunt_id else None
            if not hunt or hunt["hid"] != hid:
                raise UserError("I don't have a leak hunt open. Say 'find my leak' to start one.")
            if kind not in VALID_KINDS:
                raise UserError("I can note: everything is off, a valve closed, a valve opened, or undo.")
            topo = Topology.from_json(hunt["topology"])
            if kind in ("close", "open"):
                if not node:
                    raise UserError("Which valve?")
                node = self._resolve_nodes(topo, [node])[0]
            else:
                node = None
            if hunt["ended"] is not None:
                out = self._present(hunt_id, now)
                out["ignored"] = "This hunt is finished."
                return out
            self._poll(hid, now)
            reps = self.store.reports(hunt_id)
            if reps and reps[-1]["kind"] == kind and reps[-1]["node"] == node and now - reps[-1]["t"] < DEDUPE_S:
                out = self._present(hunt_id, now)
                out["ignored"] = "Duplicate report ignored."
                return out
            if hunt["mode"] == "auto":
                ig = self._auto_report_refusal(hid, hunt, topo, reps, kind, node, now)
                if ig:
                    out = self._present(hunt_id, now)
                    out["ignored"] = ig
                    return out
            if kind == "undo" and not reps:
                raise UserError("There's nothing to undo yet.")
            self.store.add_report(hunt_id, now, kind, node)
            src = self._source(hid)
            if kind in ("close", "open"):
                self.store.add_readings(hid, src.on_user_report(kind, node, now))
            return self._present(hunt_id, now)

    def _auto_report_refusal(self, hid, hunt, topo, reps, kind, node, now) -> str:
        """In an automatic hunt the customer only ever reports their OWN actions: 'quiet', and hand valves."""
        acts = set(self._actuated(hid, topo))
        if kind in ("close", "open") and node in acts:
            return "I'm switching that pipe myself, and I check its position, so there's nothing for you to report."
        if kind == "undo":
            if not reps or reps[-1].get("src") != "user":
                return "I made that last change myself and the pipe confirmed it, so there's nothing to undo. Say stop if you want me to put everything back."
        if kind == "quiet":
            v = self._engine(hunt, as_of=now).view(now)
            if v.action.kind != "quiet":
                return "I'm already running the test; nothing is needed from you right now."
        return ""

    def stop_hunt(self, hid: str) -> dict:
        """'Alexa, stop.' Always allowed, always idempotent: ends the hunt and puts back every pipe it closed."""
        with self._lock:
            now = self.clock()
            h = self.store.latest_hunt(hid)
            if h and h["ended"] is not None and h["outcome"] == "stopped":
                return self._present(h["id"], now)                        # a second "stop" says the same thing again
            if not h or h["ended"] is not None:
                return dict(kind="stopped", speech="There's no leak hunt running right now.",
                            simulated=bool(self._source(hid).simulated), time_scale=self.time_scale)
            self._end_hunt(h, now, "stopped")
            return self._present(h["id"], now)

    def hunt_status(self, hid: str, hunt_id: Optional[str] = None) -> dict:
        with self._lock:
            now = self.clock()
            hunt_id = hunt_id or (self.store.latest_hunt(hid) or {}).get("id")
            hunt = self.store.hunt(hunt_id) if hunt_id else None
            if not hunt or hunt["hid"] != hid:
                raise UserError("I don't have a leak hunt to show. Say 'find my leak' to start one.")
            self._poll(hid, now)
            return self._present(hunt_id, now)

    # ---------------------------------------------------------------- the automatic runner
    def tick(self) -> None:
        """Called about once a second by the server (and by tests with a fake clock). Safe to call at any time."""
        with self._lock:
            now = self.clock()
            for h in self.store.open_hunts("auto"):
                try:
                    self._poll(h["hid"], now, min_gap=3.0)
                    self._active_hunt(h["hid"], now)                    # enforces the idle timeout
                    self._drive(h["id"], now)
                    cur = self.store.hunt(h["id"])
                    if cur and cur["ended"] is None:                    # let the engine end it if it is finished
                        self._present(h["id"], self.clock())
                except UserError:
                    pass
            try:
                self.filler.tick(now)
            except Exception:                                      # a fill problem must never stop the heartbeat
                logging.getLogger("leakhunt").exception("fill tick failed")
            for hid, hub in list(self._hubs.items()):
                if hub is not None and hasattr(hub, "expire"):
                    hub.expire(now)
            for hook in list(self.tick_hooks):
                try:
                    hook()
                except Exception:                                  # an extra must never break the heartbeat
                    logging.getLogger("leakhunt").exception("tick hook failed")

    def _abort(self, hunt: dict, now: float, reason: str) -> None:
        self._end_hunt(hunt, now, "aborted:" + reason)

    def _drive(self, hunt_id: str, now: float) -> None:
        """Let Alexa do what the engine asks, when the engine asks for a pipe that the hub can move."""
        hunt = self.store.hunt(hunt_id)
        if not hunt or hunt["ended"] is not None or hunt["mode"] != "auto":
            return
        hid = hunt["hid"]
        if self._hub(hid) is None:
            self._abort(hunt, now, "no_hub")
            return
        for _ in range(8):
            now = self.clock()
            eng = self._engine(hunt, as_of=now)
            v = eng.view(now)
            if v.phase in ("done", "aborted"):
                return                                                   # _present ends it and puts the pipes back
            if (v.data_age_s is None and now - hunt["started"] > STALE_ABORT_S) or (v.data_age_s or 0.0) > STALE_ABORT_S:
                self._abort(hunt, now, "telemetry_stale")
                return
            stale = bool(v.stale)                                         # older than cfg.stale_s: suspend; STALE_ABORT_S: stop
            try:
                pos = self._positions(hid, now)
                self._hub_down_since.pop(hid, None)
            except Exception:
                since = self._hub_down_since.setdefault(hid, now)
                if now - since > HUB_GRACE_S:
                    self._abort(hunt, now, "hub_unreachable")
                return
            topo = eng.topo
            acts = self._actuated(hid, topo)
            problem = self._verify_and_renew(hunt, v, pos, acts, now, renew=not stale)
            if problem:
                self._abort(hunt, now, problem)
                return
            if stale:
                return                                                    # paused: no new valve command until data is fresh again
            a = v.action
            steps: Optional[List[Tuple[str, str]]] = None
            quiet_recheck = False
            if a.kind in ("close", "open"):
                node = (a.close or a.open)[0]
                if node in acts:
                    steps = [(a.kind, node)]
            elif a.kind == "set_state":                                   # the pipes I can move, now; hand valves stay with the customer
                steps = [("close", n) for n in a.close if n in acts] + [("open", n) for n in a.open if n in acts]
            elif a.kind == "quiet" and a.why in ("state_mismatch", "state_conflict"):
                if all(n in acts for n in v.closed):                      # every valve in question is one we can vouch for
                    quiet_recheck = True
            elif a.kind == "wait" and a.why in REMEASURE_WHY:
                # The window of the newest step includes the moment the flow changed, and more readings cannot take it out: left
                # alone the hunt would say "I'll re-measure" while a pipe stays closed until the idle timeout (30 minutes). A
                # fresh step now gives a clean window. The valve positions are not in question, so nothing needs vouching for.
                quiet_recheck = True
            if not steps and not quiet_recheck:
                return
            if quiet_recheck:
                n_re = sum(1 for r in self.store.reports(hunt_id) if r["kind"] == "quiet" and r.get("src") == "hub")
                if n_re >= MAX_AUTO_RECHECKS:
                    self._abort(hunt, now, "house_not_quiet")
                    return
                self.store.add_report(hunt_id, self.clock(), "quiet", None, src="hub")
                continue
            if not self._actuate(hunt, steps):
                return

    def _verify_and_renew(self, hunt: dict, v, pos: dict, acts: List[str], now: float, renew: bool = True) -> str:
        """Every pipe must be where the engine believes it is; every closed pipe keeps a live lease (not renewed while
        the telemetry is stale: a hunt that cannot see must not extend what it closed)."""
        closed = set(v.closed)
        for p in acts:
            st = pos.get(p)
            if st is None:
                return "pipe_missing:" + p
            if st.position == "fault":
                return "pipe_fault:" + p
            if st.position == "moving":
                continue
            want = CLOSED if p in closed else OPEN
            if st.position != want:
                return "pipe_moved:" + p
            if renew and want == CLOSED and (st.lease_left_s is None or st.lease_left_s < LEASE_S * LEASE_RENEW_FRAC):
                ack = self._command(hunt["hid"], hunt["id"], p, CLOSED, "hunt-renew", LEASE_S, force=True, audit=False)
                if not ack.ok:
                    return "lease_renew_failed:" + p
        return ""

    def _actuate(self, hunt: dict, steps: List[Tuple[str, str]]) -> bool:
        hid, hid_hunt = hunt["hid"], hunt["id"]
        if self.store.count_actuations(hid_hunt) + len(steps) > MAX_COMMANDS:
            self._abort(hunt, self.clock(), "command_cap")
            return False
        acks: List[Tuple[str, str, Ack]] = []
        for kind, node in steps:
            want = CLOSED if kind == "close" else OPEN
            ack = self._command(hid, hid_hunt, node, want, "hunt", LEASE_S if want == CLOSED else None)
            acks.append((kind, node, ack))
            if not ack.ok:
                self._abort(hunt, self.clock(), f"pipe_fault:{node}")
                return False
            if ack.t_ack - ack.t_cmd > self.cfg.act_guard_s:
                self._abort(hunt, self.clock(), f"pipe_too_slow:{node}")
                return False
        for kind, node, ack in acks:                                     # the engine hears about it when the pipe confirmed it
            self.store.add_report(hid_hunt, ack.t_ack, kind, node, src="hub")
        return True

    def _end_hunt(self, hunt: dict, t: float, outcome: str, js: Optional[dict] = None) -> None:
        if self.store.end_hunt(hunt["id"], t, outcome) and hunt["mode"] == "auto":
            self._on_end(hunt, outcome, js, t)

    def _on_end(self, hunt: dict, outcome: str, js: Optional[dict], t: float) -> None:
        """Runs exactly once when an automatic hunt ends, however it ends: put every pipe back.

        The one pipe not simply put back is a confirmed culprit of a finished hunt. It stays closed on a FRESH lease (the
        hub reopens it by itself) while the customer is asked whether to keep it closed. Only a spoken or tapped yes turns
        that into a latch (set_pipe); "no" or silence lets it reopen."""
        hid = hunt["hid"]
        topo = Topology.from_json(hunt["topology"])
        acts = self._actuated(hid, topo)
        hold: Dict[str, Optional[float]] = {}
        if outcome in ("only_sources", "partial"):
            if js is None:
                js = self._engine(hunt, as_of=t).view(t).to_json()
            hold = {c["node"]: c.get("lpm") for c in js["claims"] if c["kind"] == "source_confirmed" and c["node"] in acts}
        pos: dict = {}
        try:
            pos = self._positions(hid, self.clock())
        except Exception:
            pass
        for p in acts:
            if p in hold:
                ack = self._command(hid, hunt["id"], p, CLOSED, "hunt-end", OFFER_S, force=True)
                if ack.ok:
                    self.store.set_offer(hid, p, t, max(t, ack.t_ack) + OFFER_S, hunt["id"], hold[p])
                continue
            if p in pos and pos[p].position == OPEN:
                continue
            self._command(hid, hunt["id"], p, OPEN, "hunt-end", None, force=True)

    # ---------------------------------------------------------------- engine plumbing
    def _engine(self, hunt: dict, as_of: Optional[float] = None, cls=HuntEngine) -> HuntEngine:
        eng = cls(Topology.from_json(hunt["topology"]), self.cfg)
        hi = as_of if as_of is not None else 1e18
        for r in self.store.readings(hunt["hid"], since=hunt["started"] - 120.0, until=hi):
            eng.ingest(r)
        for rep in self.store.reports(hunt["id"]):
            if rep["t"] <= hi:
                eng.report(rep["t"], rep["kind"], rep["node"])
        return eng

    def _trace(self, hunt: dict, now: float, topo: Topology) -> dict:
        """Recent flow as stepped intervals plus the customer's steps, for the live sparkline."""
        hid = hunt["hid"]
        rs = self.store.readings(hid, since=max(hunt["started"] - 60.0, now - 160.0), until=now)
        pts = []
        for a, b in zip(rs, rs[1:]):
            if a.boot_id != b.boot_id or b.uptime_ms <= a.uptime_ms or b.out_total_l < a.out_total_l:
                continue
            lpm = (b.out_total_l - a.out_total_l) / ((b.uptime_ms - a.uptime_ms) / 60000.0)
            pts.append([round(a.t - now, 1), round(b.t - now, 1), round(lpm, 3)])
        pts = [p for p in pts if p[1] >= -150.0]
        first = pts[0][0] if pts else 0.0
        span = float(min(150.0, max(60.0, -first)))
        marks = []
        for r in self.store.reports(hunt["id"]):
            if r["t"] > now or r["t"] - now < -span:
                continue
            who = "Alexa " if r.get("src") == "hub" else ""
            lab = {"quiet": "all off", "undo": "undo"}.get(r["kind"]) or (
                (who + "closed " if r["kind"] == "close" else who + "opened ") + topo.label(r["node"]))
            marks.append(dict(t=round(r["t"] - now, 1), label=lab))
        return dict(span_s=span, points=pts, marks=marks, floor_lpm=self.cfg.floor_lpm)

    def _status(self, hunt: dict, v) -> str:
        oc = hunt.get("outcome") or ""
        if hunt["ended"] is not None and oc == "stopped":
            return "stopped"
        if oc.startswith("aborted:") or (hunt["ended"] is not None and oc == "abandoned") or v.phase == "aborted":
            return "aborted"
        if v.phase == "done" or hunt["ended"] is not None:
            return "done"
        return "active"

    def _moves_text(self, hid: str, hunt_id: str, topo: Topology) -> str:
        """'I closed the toilet.' - the pipes Alexa moved in the most recent step."""
        rows = [r for r in self.store.actuations(hid, hunt_id) if r["source"] == "hunt" and r["ok"]]
        if not rows:
            return ""
        t = rows[-1]["t"]
        grp = [r for r in rows if t - r["t"] <= 3.0]
        closed = ["the " + topo.label(r["pipe"]) for r in grp if r["want"] == CLOSED]
        opened = ["the " + topo.label(r["pipe"]) for r in grp if r["want"] == OPEN]
        parts = []
        if closed:
            parts.append("closed " + join_or(closed, "and"))
        if opened:
            parts.append("reopened " + join_or(opened, "and"))
        return "I " + " and ".join(parts) + "."

    def _not_restored(self, hunt: dict, topo: Topology, acts: List[str], now: float, intended_closed: set) -> List[dict]:
        """Pipes this hunt closed that are not confirmed open again, shortly after it ended. Stop and the end of a hunt put
        pipes back on a best-effort basis; whatever could not be confirmed is reported by name, with the latest moment the
        hub's own lease reopens it."""
        if hunt["ended"] is None or now - hunt["ended"] > LEASE_S + 30.0:
            return []
        hid = hunt["hid"]
        rows = [r for r in self.store.actuations(hid, hunt["id"]) if r["ok"] and r["pipe"] in acts]
        last_close: Dict[str, float] = {}
        last_open: Dict[str, float] = {}
        for r in rows:
            (last_close if r["want"] == CLOSED else last_open)[r["pipe"]] = r["t"]
        theirs = {r["pipe"] for r in self.store.actuations(hid)                      # the customer's own spoken command, after the hunt ended,
                  if r["ok"] and r["source"] == "voice" and r["t"] >= hunt["ended"]}  # is theirs: not something this hunt left behind
        try:
            pos: Optional[dict] = self._positions(hid, now)
        except Exception:
            pos = None
        out = []
        for p, tc in sorted(last_close.items()):
            if p in intended_closed or p in theirs:
                continue
            if pos is not None:
                st = pos.get(p)
                if st is not None and st.position == OPEN:
                    continue
                left = LEASE_S if st is None or st.lease_left_s is None else st.lease_left_s
            else:
                if last_open.get(p, -1.0) > tc:
                    continue
                left = max(0.0, tc + LEASE_S - now)
            out.append(dict(id=p, label=topo.label(p), reopens_within_s=round(max(0.0, left), 1)))
        return out

    def _auto_speech(self, hunt: dict, v, js: dict, topo: Topology, status: str, simulated: bool, had_reports: bool,
                     needs_hands: bool = True) -> str:
        act = js["action"]
        text = act["text"]
        if status == "active":
            if not had_reports:
                return ("This is a simulated house for the demo. " if simulated else "") + AUTO_INTRO
            moved = self._moves_text(hunt["hid"], hunt["id"], topo)
            if act["kind"] == "wait":
                return (moved + " " if moved else "") + text.replace("your last step", "that change")
            if not needs_hands:                                          # the hub is about to move it: never ask a person to
                return (moved + " " if moved else "") + "I'm moving the next pipe now."
            return "I need your hands for this one. " + text
        return text

    def _present(self, hunt_id: str, now: float) -> dict:
        hunt = self.store.hunt(hunt_id)
        if hunt["mode"] == "auto" and hunt["ended"] is None:
            self._drive(hunt_id, now)
            hunt = self.store.hunt(hunt_id)
            now = max(now, self.clock())            # what _drive just did happened a moment ago: the view must include it
        src = self._source(hunt["hid"])
        now_eff = hunt["ended"] if hunt["ended"] is not None else now
        eng = self._engine(hunt, as_of=now_eff)
        v = eng.view(now_eff)
        js = v.to_json()
        if v.phase in ("done", "aborted") and hunt["ended"] is None:
            self._end_hunt(hunt, now, v.action.outcome or v.phase, js)
            hunt = self.store.hunt(hunt_id)
        status = self._status(hunt, v)
        topo = eng.topo
        auto = hunt["mode"] == "auto"
        positions: dict = {}
        if auto:
            try:
                positions = self._positions(hunt["hid"], now)
            except Exception:
                positions = {}
        acts = set(self._actuated(hunt["hid"], topo)) if auto else set()
        latched = {l["pipe"] for l in self.store.latches(hunt["hid"])}
        nodes = []
        for n in topo.nodes.values():
            verdict = js["verdicts"].get(n.id, "untested")
            st = positions.get(n.id)
            closed = n.id in v.closed
            if auto and status != "active" and n.id in acts and st is not None:     # after the hunt: where the pipe really is now
                closed = st.position == CLOSED
            nodes.append(dict(id=n.id, label=n.label, parent=n.parent, how=n.how, verdict=verdict,
                              state="closed" if closed else "open", leaf=topo.is_leaf(n.id), room=n.room,
                              number=n.number, actuated=n.id in acts, latched=n.id in latched,
                              position=None if st is None else st.position))
        found = [c["node"] for c in js["claims"] if c["kind"] == "source_confirmed"]
        speech = js["action"]["text"]
        had_reports = bool(self.store.reports(hunt_id))
        action, phase = js["action"], v.phase
        outcome = hunt["outcome"] if hunt["ended"] is not None else (v.action.outcome or None)
        if status in ("stopped", "aborted") and (hunt.get("outcome") or "") and v.phase != "aborted":
            oc = hunt["outcome"]
            code, _, pipe = oc.removeprefix("aborted:").partition(":") if oc.startswith("aborted:") else (oc, "", "")
            if oc == "stopped":
                text = "Stopped. I've put back every pipe I closed." if auto else "Stopped. The hunt is closed."
                if not auto and v.closed:                       # what the person closed by hand is theirs to put back: say so
                    text += f" You can open {join_or(['the ' + topo.label(n) + ' valve' for n in v.closed if n in topo.nodes], 'and')} again now."
            elif oc == "abandoned":
                text = "This hunt sat idle for too long, so I closed it" + (" and put every pipe back." if auto else ".")
            else:
                text = ABORT_TEXT.get(code, "I stopped the test and put everything back.").format(pipe=topo.label(pipe) if pipe in topo.nodes else "A pipe")
            action = dict(kind="abort", text=text, close=[], open=[], eta_s=None, why=code, outcome=oc)
            speech, phase = text, "aborted" if status == "aborted" else "stopped"
        elif auto:
            hand_now = [n for n in list(action.get("close") or []) + list(action.get("open") or []) if n not in acts]
            speech = self._auto_speech(hunt, v, js, topo, status, bool(src.simulated), had_reports,
                                       needs_hands=action["kind"] == "quiet" or bool(hand_now))
        elif src.simulated and not had_reports:
            speech = "This is a simulated house for the demo. " + speech      # spoken AND shown, never only one
        if status != "active" and v.phase == "done" and v.action.outcome in ("only_sources", "partial") and phase == "done":
            lead = [c["text"] for c in js["claims"] if c["kind"] == "source_confirmed"]
            speech = " ".join(lead + [speech])             # the finding is spoken, not just the proof of completeness
        offered: List[str] = []
        offs: dict = {}
        if auto and status == "done":
            offs = {o["pipe"]: o for o in self.store.offers(hunt["hid"], now) if o.get("hunt_id") == hunt_id}
            offered = [p for p in found if p in offs]
            kept = [p for p in sorted(latched) if p in found]
            speech = re.sub(r" Leave (the .*? valve|those valves) closed until (it's|they're) repaired\.", "", speech)
            if offered:                                       # a temporary hold, with a question: NOT yet kept closed
                one = len(offered) == 1
                speech += (f" I'm holding {join_or(['the ' + topo.label(p) for p in offered], 'and')} off for up to two minutes "
                           f"while you decide. Do you want {'it' if one else 'them'} kept off until {'it has' if one else 'they have'} "
                           f"been checked? Say 'keep it off', or 'turn it back on'. If I don't hear from you, your pipe controller "
                           f"turns {'it' if one else 'them'} back on by itself, and everything else is already on.")
            elif kept:
                lab = [topo.label(p) for p in kept]
                speech += (f" I've kept the {join_or(lab, 'and')} closed, as you asked, and turned everything else back on. "
                           f"When it's repaired, say 'turn on the {lab[0]}'.")
            else:
                speech += " Everything is back on."
        if not auto and status == "done" and v.phase == "done":
            reopen = [n for n in js["closed"] if n not in found and n in topo.nodes]   # a guided hunt leaves what the person closed
            if reopen:
                speech += f" You can open {join_or(['the ' + topo.label(n) + ' valve' for n in reopen], 'and')} again now."
        if auto and status == "aborted" and v.phase == "aborted":
            speech += " I've put every pipe I closed back."
        for nd in nodes:
            nd["offered"] = nd["id"] in offered
        paused = auto and status == "active" and bool(v.stale)
        if paused:
            speech = PAUSE_TEXT
        not_restored = self._not_restored(hunt, topo, acts, now, set(offs) | set(latched)) if auto and status != "active" else []
        if not_restored:
            for said, done in RESTORE_CLAIMS:
                if said in speech:
                    speech = speech.replace(said, done)
                    break
            names = join_or(["the " + r["label"] for r in not_restored], "and")
            secs = int(max(r["reopens_within_s"] for r in not_restored))
            speech += (f" I couldn't confirm {names} {'is' if len(not_restored) == 1 else 'are'} back on yet. Your pipe controller "
                       f"reopens {'it' if len(not_restored) == 1 else 'them'} by itself within {secs} seconds at the latest.")
        auto_info = None
        if auto:
            n_cmd = self.store.count_actuations(hunt_id)
            hand = [n for n in list(action.get("close") or []) + list(action.get("open") or []) if n not in acts]
            auto_info = dict(running=status == "active", needs_you=status == "active" and (action["kind"] == "quiet" or bool(hand)),
                             commands=n_cmd, max_commands=MAX_COMMANDS, lease_s=LEASE_S,
                             closed_by_alexa=[dict(id=n["id"], label=n["label"]) for n in nodes if n["state"] == "closed" and n["actuated"]],
                             kept_closed=[dict(id=p, label=topo.label(p)) for p in sorted(latched) if p in topo.nodes and status == "done"],
                             isolation_offer=[dict(id=p, label=topo.label(p), expires_in_s=round(max(0.0, offs[p]["expires"] - now), 1))
                                              for p in offered],
                             paused=None if not paused else dict(
                                 data_age_s=None if v.data_age_s is None else round(v.data_age_s, 1),
                                 stops_in_s=round(max(0.0, STALE_ABORT_S - (v.data_age_s if v.data_age_s is not None
                                                                           else now - hunt["started"])), 1)),
                             not_restored=not_restored, offer_s=OFFER_S,
                             moves=self._moves_text(hunt["hid"], hunt_id, topo))
        return dict(kind="hunt", hunt_id=hunt_id, mode=hunt["mode"], status=status, phase=phase, simulated=src.simulated,
                    time_scale=self.time_scale, floor_lpm=self.cfg.floor_lpm, trace=self._trace(hunt, now_eff, topo),
                    speech=speech, action=action, claims=js["claims"], nodes=nodes, auto=auto_info,
                    closed=js["closed"], found=found, flow_now_lpm=None if v.flow_now_lpm is None else round(v.flow_now_lpm, 3),
                    flow_text=None if v.flow_now_lpm is None else fmt_lpm(v.flow_now_lpm), data_age_s=None if v.data_age_s is None else round(v.data_age_s, 1),
                    stale=v.stale, steps_done=v.steps_done, est_steps_left=v.est_steps_left,
                    elapsed_s=round(now_eff - hunt["started"], 1), outcome=outcome,
                    evidence_available=status != "active")


    # ================================================================ evidence
    def evidence(self, hid: str, hunt_id: Optional[str] = None) -> dict:
        with self._lock:
            hunt_id = hunt_id or (self.store.latest_hunt(hid) or {}).get("id")
            hunt = self.store.hunt(hunt_id) if hunt_id else None
            if not hunt or hunt["hid"] != hid:
                raise UserError("There's no hunt to write up yet.")
            if hunt["ended"] is None:
                self._present(hunt_id, self.clock())            # may end it
                hunt = self.store.hunt(hunt_id)
            if hunt["ended"] is None:
                raise UserError("The hunt isn't finished yet, so there's nothing final to write up.")
            as_of = hunt["ended"]
            eng = self._engine(hunt, as_of=as_of)
            v = eng.view(as_of)
            src = self._source(hid)
            body = dict(
                schema="leakhunt.evidence/1", engine=ENGINE_VERSION, hunt_id=hunt_id, household=self.household(hid)["name"],
                simulated=bool(src.simulated), as_of=as_of, started=hunt["started"], outcome=hunt["outcome"],
                mode=hunt["mode"], consent_t=hunt.get("consent_t"),
                actuations=[dict(t=a["t"], pipe=a["pipe"], want=a["want"], source=a["source"], ok=a["ok"], detail=a["detail"])
                            for a in self.store.actuations(hid, hunt_id)],
                config=asdict(self.cfg), topology=hunt["topology"],
                reports=[dict(t=r["t"], kind=r["kind"], node=r["node"], by=r.get("src", "user")) for r in self.store.reports(hunt_id)],
                readings=[dict(sample_id=r.sample_id, boot_id=r.boot_id, uptime_ms=r.uptime_ms, t=r.t, total_l=r.out_total_l,
                               valve=r.valve, leak=r.leak) for r in self.store.readings(hid, since=hunt["started"] - 120.0, until=as_of)],
                result=v.to_json(), assumptions=list(self.assumptions))
            digest = hashlib.sha256(_canon(body)).hexdigest()
            mac = hmac.new(self.key, digest.encode(), hashlib.sha256).hexdigest()
            packet = dict(body, sha256=digest, hmac_sha256=mac)
            return packet

    def verify_packet(self, packet: dict) -> dict:
        """Check integrity (hash, and HMAC if we hold the key) and re-derive the result from the raw data."""
        body = {k: v for k, v in packet.items() if k not in ("sha256", "hmac_sha256")}
        digest_ok = hashlib.sha256(_canon(body)).hexdigest() == packet.get("sha256")
        mac_ok = hmac.compare_digest(hmac.new(self.key, str(packet.get("sha256")).encode(), hashlib.sha256).hexdigest(),
                                     str(packet.get("hmac_sha256")))
        cfg = Config(**body["config"])
        eng = HuntEngine(Topology.from_json(body["topology"]), cfg)
        for r in body["readings"]:
            eng.ingest(Reading(r["sample_id"], r["boot_id"], r["uptime_ms"], r["t"], r["total_l"], r["valve"], r["leak"]))
        for r in body["reports"]:
            eng.report(r["t"], r["kind"], r["node"])
        replay = eng.view(body["as_of"]).to_json()
        return dict(hash_ok=digest_ok, hmac_ok=mac_ok, replay_matches=replay == body["result"])

    def evidence_token(self, hunt_id: str) -> str:
        return hmac.new(self.key, b"evidence:" + hunt_id.encode(), hashlib.sha256).hexdigest()[:24]

    def evidence_url(self, hunt_id: str) -> Optional[str]:
        if not self.public_base_url:
            return None
        return f"{self.public_base_url}/evidence/{hunt_id}?k={self.evidence_token(hunt_id)}"

    def evidence_html(self, packet: dict) -> str:
        return render_evidence_html(packet)


def render_evidence_html(p: dict) -> str:
    esc = html.escape
    res = p["result"]
    topo = {n["id"]: n for n in p["topology"]}
    label = lambda i: topo[i]["label"] if i in topo else i
    claims = "".join(f"<li class='{esc(c['level'])}'><b>{esc(c['level'].upper())}</b> {esc(c['text'])}</li>" for c in res["claims"])
    steps = "".join(
        f"<tr><td>{i + 1}</td><td>{esc(_clock(r['t']))}</td><td>{esc(r['kind'])}</td><td>{esc(label(r['node']) if r['node'] else '—')}</td>"
        f"<td>{'Alexa, via the pipe hub' if r.get('by') == 'hub' else 'customer'}</td></tr>"
        for i, r in enumerate(p["reports"]))
    acts = "".join(
        f"<tr><td>{esc(_clock(a['t']))}</td><td>{esc(label(a['pipe']))}</td><td>{esc(a['want'])}</td><td>{esc(a['source'])}</td>"
        f"<td>{'ok' if a['ok'] else esc('failed: ' + a['detail'])}</td></tr>" for a in p.get("actuations", []))
    auto_note = ("<div class='sim'>AUTOMATIC HUNT — Alexa operated the pipes through the pipe hub after the customer agreed. "
                 "Every valve command is listed below.</div>") if p.get("mode") == "auto" else ""
    wins = "".join(
        f"<tr><td>{e['idx']}</td><td>{esc(', '.join(label(x) for x in e['closed']) or 'nothing')}</td><td>{e['T']:.0f} s</td>"
        f"<td>{e['lpm']:.2f} L/min</td><td>{esc(e['status'])}</td></tr>" for e in res["epochs"])
    ass = "".join(f"<li>{esc(a)}</li>" for a in p["assumptions"])
    chart = _evidence_svg(p, label)
    banner = ("<div class='sim'>SIMULATED HOUSE — this report was produced from a software model, not real plumbing."
              "</div>") if p["simulated"] else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Leak hunt evidence {esc(p['hunt_id'])}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#14181e}}
h1{{font-size:1.4rem}}h2{{font-size:1.1rem;margin-top:1.6rem}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #c8ccd2;padding:.3rem .5rem;text-align:left}}.sim{{background:#fff3cd;border:2px solid #b58100;padding:.6rem;font-weight:700}}
li.confirmed b{{color:#0b6b2f}}li.warning b{{color:#9a4b00}}code{{word-break:break-all}}</style></head><body>
<h1>Leak hunt evidence</h1>{banner}{auto_note}
<p>Hunt <code>{esc(p['hunt_id'])}</code> · household {esc(p['household'])} · outcome <b>{esc(str(p['outcome']))}</b></p>
<h2>What was found</h2><ul>{claims or '<li>No claims.</li>'}</ul>
<h2>Steps taken</h2><table><tr><th>#</th><th>Time (UTC)</th><th>Step</th><th>Valve</th><th>By</th></tr>{steps}</table>
{('<h2>Valve commands sent</h2><table><tr><th>Time (UTC)</th><th>Pipe</th><th>Wanted</th><th>Why</th><th>Result</th></tr>' + acts + '</table>') if acts else ''}
<h2>Flow during the hunt</h2>{chart}
<h2>Measurement windows</h2><table><tr><th>#</th><th>Closed</th><th>Length</th><th>Flow</th><th>Status</th></tr>{wins}</table>
<h2>Assumptions and limits</h2><ul>{ass}</ul>
<h2>Integrity</h2><p>SHA-256 <code>{esc(p['sha256'])}</code><br>HMAC-SHA-256 <code>{esc(p['hmac_sha256'])}</code></p>
<p>The raw readings and reports are in the JSON packet. The engine is deterministic: replaying them gives this result.
<b>What this establishes:</b> the record was not altered after it was written (the codes above), and the same readings and steps give
the same result when replayed. <b>What it does not:</b> that the diagnosis is physically true. A sensor error, a valve that was not
the one reported, or a pipe that did not move would be preserved exactly as recorded.</p>
</body></html>"""


def _clock(t: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%S")


def _evidence_svg(p: dict, label) -> str:
    """Flow (L/min) over the whole hunt with the customer's steps marked. Self-contained, no scripts."""
    esc = html.escape
    rs = p["readings"]
    pts = []
    for a, b in zip(rs, rs[1:]):
        if a["boot_id"] != b["boot_id"] or b["uptime_ms"] <= a["uptime_ms"] or b["total_l"] < a["total_l"]:
            continue
        pts.append((a["t"], b["t"], (b["total_l"] - a["total_l"]) / ((b["uptime_ms"] - a["uptime_ms"]) / 60000.0)))
    if not pts:
        return "<p>No flow data.</p>"
    t0, t1 = pts[0][0], pts[-1][1]
    ymax = max(3.0, max(q for _, _, q in pts) * 1.2)
    W, H, L, B, T = 640, 220, 40, 26, 14
    X = lambda t: L + (t - t0) / max(t1 - t0, 1.0) * (W - L - 10)
    Y = lambda v: H - B - v / ymax * (H - B - T)
    d = f"M{X(pts[0][0]):.1f},{Y(pts[0][2]):.1f}"
    for a, b, q in pts:
        d += f" L{X(a):.1f},{Y(q):.1f} L{X(b):.1f},{Y(q):.1f}"
    floor = p["config"]["floor_lpm"]
    marks = ""
    for i, r in enumerate(p["reports"]):
        x = X(r["t"])
        lab = {"quiet": "all off", "undo": "undo"}.get(r["kind"]) or (("closed " if r["kind"] == "close" else "opened ") + label(r["node"]))
        marks += (f"<line x1='{x:.1f}' x2='{x:.1f}' y1='{T}' y2='{H - B}' stroke='#6b7280' stroke-dasharray='3 3'/>"
                  f"<text x='{x + 3:.1f}' y='{T + 10 + (i % 3) * 12}' font-size='11' fill='#374151'>{i + 1}. {esc(lab)}</text>")
    return (f"<svg viewBox='0 0 {W} {H}' width='100%' role='img' aria-label='Flow in litres per minute over the hunt, with each step marked'>"
            f"<rect width='{W}' height='{H}' fill='#fff' stroke='#c8ccd2'/>"
            f"<line x1='{L}' x2='{W - 10}' y1='{Y(floor):.1f}' y2='{Y(floor):.1f}' stroke='#b3261e' stroke-dasharray='5 4'/>"
            f"<text x='{L + 4}' y='{Y(floor) - 4:.1f}' font-size='11' fill='#b3261e'>sensor floor {floor:.1f} L/min</text>"
            f"<path d='{d}' fill='none' stroke='#00607f' stroke-width='2.5'/>{marks}"
            f"<text x='4' y='{T + 4}' font-size='11' fill='#374151'>{ymax:.1f}</text><text x='4' y='{H - B}' font-size='11' fill='#374151'>0</text>"
            f"<text x='{L}' y='{H - 6}' font-size='11' fill='#374151'>L/min over {t1 - t0:.0f} s</text></svg>")
