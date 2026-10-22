"""Power Hunt: the leak-hunt engine on a whole-home energy meter and smart plugs. SIMULATED in this repository.

"Alexa, what's using power while we sleep?" A home's overnight minimum is power that runs around the clock. Power Hunt finds
which smart plug it is behind the same way Leak Hunt finds a leaking pipe: switch one branch off, read the meter, switch it back
on, and name a plug only when the drop comes back with matching size.

Nothing in huntkit changes. The meter's running total is kept in watt-minutes, so the engine's "per minute" rate is watts; the
branches are smart plugs; plugs rest ON and every switch-off during a hunt is a lease the plug controller undoes by itself.
Plugs marked `protect` (a router, a freezer) are never switched and are not part of a hunt at all. The words the customer hears
are translated by addon/vocab.py, and a test proves no water wording reaches them.

What is not proven here: no real energy meter or smart plug has been connected. Every number below is a model or an assumption.
"""
from __future__ import annotations

import threading
from functools import wraps
from typing import Dict, List, Optional

from huntkit.baseline import analyse
from huntkit.core import Config, Node, Reading, ROOT, Topology
from huntkit.pipes import join_or
from huntkit.sim import synth_history

from . import vocab
from .service import HuntService, UserError

MUTANT: Optional[str] = None       # deliberately broken variants for tests/test_power.py: "no_translate", "protected_in_hunt"

# Engineering assumptions until measured on a real meter: the meter resolves 1 watt-second; window-to-window variability of the
# overnight base load is 2 % (a fridge or freezer compressor cycling breaks that, and the engine then calls the window unstable and
# re-measures); 5 W is the smallest always-on draw it claims to see.
POWER_CFG = Config(ppl=60.0, floor_lpm=5.0, noise_pps=0.02, rel_sd=0.02)

DEMO_BASE_W = 62.0                 # simulated: what the home draws all night with every switchable plug off
DEMO_EXTRA_W = 46.0                # simulated: what has been on behind the TV plug for the last few nights
DEMO_SOURCES = {"tv": DEMO_EXTRA_W, ROOT: DEMO_BASE_W}     # a source keyed ROOT is upstream of every plug: never switched off


def power_topology() -> Topology:
    return Topology([
        Node("tv", "TV plug", ROOT, prior=3, how="behind the TV stand", room="living room", aliases=("tv", "television", "tv corner")),
        Node("desk", "desk plug", ROOT, prior=2, how="under the desk", room="study", aliases=("computer", "desk", "monitor")),
        Node("counter", "kitchen counter plug", ROOT, prior=1, how="by the coffee machine", room="kitchen",
             aliases=("kitchen", "coffee machine", "counter")),
        Node("bedside", "bedside plug", ROOT, prior=1, how="behind the bed", room="bedroom", aliases=("bedroom", "charger", "lamp")),
        Node("router", "router plug", ROOT, how="in the hall cupboard", room="hall", aliases=("router", "wifi", "internet"), protect=True),
        Node("freezer", "garage freezer plug", ROOT, how="in the garage", room="garage", aliases=("freezer",), protect=True),
    ])


POWER_ASSUMPTIONS = [
    "The energy meter reports a running total of energy; power is derived from differences of those totals, so the 10-second "
    "telemetry cadence loses no energy. In the evidence packet the field total_l holds watt-minutes for this meter.",
    "Only an always-on draw at or above the meter floor (5 W, an assumption) can be located.",
    "Plugs the household marked as must-stay-on (a router, a freezer) are never switched and are not part of the hunt; whatever "
    "they draw stays in the base the hunt cannot place.",
    "In an automatic hunt the plug states come from the plug controller and are assumed truthful.",
    "A load that switches itself on and off (a fridge compressor, a heater thermostat) can look like a plug effect; the engine "
    "tests window stability and requires an off-on-off replication before it says 'confirmed'.",
    "A plug that changed the draw by less than the bound it states is not excluded from drawing that much. 'No change' means "
    "nothing larger than the stated bound is behind it.",
    "Thresholds (floor, variability) are engineering assumptions until measured on a real meter.",
]


def synth_power_history(t_end: float, *, nights: int = 14, new_nights: int = 3, base_w: float = DEMO_BASE_W,
                        extra_w: float = DEMO_EXTRA_W, cfg: Config = POWER_CFG, tz_min: int = 330) -> List[Reading]:
    """Closed-form overnight history (00:00-06:00 local), clearly synthetic: `base_w` every night, plus `extra_w` from 02:10 on
    the night `new_nights` ago and ever since. Totals are watt-minutes."""
    total_nights = nights + 1
    rs = synth_history(t_end, nights=nights, leak_from_night=(total_nights - new_nights) if new_nights else None,
                       leak_lpm=extra_w, cfg=cfg, tz_min=tz_min, include_today=True, boot_id="hist-meter")
    out = []
    for r in rs:
        total = r.out_total_l + base_w * r.uptime_ms / 60000.0
        out.append(Reading(r.sample_id, r.boot_id, r.uptime_ms, r.t, round(int(total * cfg.ppl) / cfg.ppl, 3)))
    return out


def _watts(x: float) -> str:
    return f"{x:.0f} watts" if x >= 9.95 or x == 0 else f"{x:.1f} watts"


def _count(n: int) -> str:
    return ("zero one two three four five six seven eight nine ten".split() + [str(n)])[min(n, 11)] if n <= 10 else str(n)


def _translated(fn):
    """Public calls answer in power words. Internal calls between methods stay raw, so nothing is translated twice."""
    @wraps(fn)
    def run(self, *a, **k):
        tl = self._tl
        depth = getattr(tl, "depth", 0)
        tl.depth = depth + 1
        try:
            out = fn(self, *a, **k)
        except UserError as e:
            if depth or MUTANT == "no_translate":
                raise
            msg = vocab.translate(str(e))
            raise UserError(msg[:1].upper() + msg[1:]) from None
        finally:
            tl.depth = depth
        if depth or not isinstance(out, dict):
            return out
        out = dict(out, utility="power")
        return out if MUTANT == "no_translate" else vocab.to_power(out)
    return run


class PowerService(HuntService):
    utility = "power"
    hunt_prefix = "p_"
    hunt_skips_protected = True
    assumptions = POWER_ASSUMPTIONS

    def __init__(self, store, clock, *, key: bytes = b"dev-key", public_base_url: str = "", time_scale: float = 1.0):
        super().__init__(store, clock, POWER_CFG, key=key, public_base_url=public_base_url, time_scale=time_scale)
        self._tl = threading.local()
        if MUTANT == "protected_in_hunt":
            self.hunt_skips_protected = False

    # ---------------------------------------------------------------- the simulated home
    def ensure_demo_household(self, hid: str = "default", *, sources: Optional[Dict[str, float]] = None, nights: int = 14,
                              new_nights: int = 3, tz_min: int = 330, control: bool = True, **_ignored) -> dict:
        """A SIMULATED home: a virtual energy meter, six smart plugs (two never switched) and synthetic overnight history."""
        with self._lock:
            if self.store.household(hid):
                return self.household(hid)
            sources = dict(DEMO_SOURCES if sources is None else sources)
            topo = power_topology()
            src = {"kind": "virtual", "sources": sources}
            if control:
                src["pipehub"] = {"kind": "virtual", "enabled": True, "pipes": list(topo.nodes)}
            self.set_household(hid, "Demo home (simulated)", topo.to_json(), tz_min=tz_min, source=src)
            base = float(sources.get(ROOT, 0.0))
            extra = sum(v for k, v in sources.items() if k != ROOT)
            self.store.add_readings(hid, synth_power_history(self.clock(), nights=nights, new_nights=new_nights if extra else 0,
                                                             base_w=base, extra_w=extra, cfg=self.cfg, tz_min=tz_min))
            return self.household(hid)

    def _protected_labels(self, hid: str) -> List[str]:
        return [n.label for n in self._topology(hid).nodes.values() if n.protect]

    # ---------------------------------------------------------------- what the meter says
    def _power_night(self, hid: str, now: float) -> dict:
        h = self.household(hid)
        rep = analyse(self.store.readings(hid, since=now - 15 * 86400), self.cfg, tz_min=h["tz_min"])
        good = [n for n in rep.nights if n.min_lpm is not None and n.coverage >= 0.6]
        nights = [dict(date=n.date, min_w=None if n.min_lpm is None else round(n.min_lpm, 1), coverage=round(n.coverage, 2),
                       min_lpm=None if n.min_lpm is None else round(n.min_lpm, 1))      # the screen's chart reads min_lpm: here, watts
                  for n in rep.nights[-14:]]
        if len(good) < 2:
            return dict(verdict="insufficient_data", nights=nights, latest_w=None, usual_w=None, extra_w=None, run=0,
                        text="I don't have enough complete overnight readings yet to tell what runs all night.")
        latest = good[-1].min_lpm
        import statistics
        prev = [n.min_lpm for n in good[:-1]]
        usual = statistics.median(prev)
        mad = statistics.median(abs(x - usual) for x in prev) * 1.4826
        thr = usual + max(3 * mad, 0.1 * usual, self.cfg.floor_lpm)
        run = 0
        for n in reversed(good):
            if n.min_lpm > thr:
                run += 1
            else:
                break
        text = f"Last night your home never drew less than {_watts(latest)}."
        if latest > thr and run < len(good):
            usual_before = statistics.median([n.min_lpm for n in good[:len(good) - run]]) if run else usual
            ago = "last night" if run == 1 else f"{_count(run)} nights ago"
            text += (f" Until {ago} that was about {_watts(usual_before)}, so about {_watts(latest - usual_before)} more has been "
                     f"on all night since then.")
            verdict, extra = "new_always_on", latest - usual_before
        else:
            text += f" That's in line with your usual {_watts(usual)}."
            verdict, extra = "steady", None
        text += (f" That much runs around the clock: if it stayed at {_watts(latest)} all year, it would be about "
                 f"{latest * 8.76:.0f} kilowatt-hours.")
        return dict(verdict=verdict, nights=nights, latest_w=round(latest, 1), usual_w=round(usual, 1),
                    extra_w=None if extra is None else round(extra, 1), run=run, text=text)

    def water_status(self, hid: str) -> dict:          # the 'utility=power' answer to "what's using power while we sleep?"
        with self._lock:
            now = self.clock()
            err = self._poll(hid, now)
            h = self.household(hid)
            src = self._source(hid)
            last = self.store.readings(hid, since=now - 600)
            age = (now - last[-1].t) if last else None
            stale = age is None or age > self.cfg.stale_s
            draw = None
            if not stale and len(last) >= 2:
                a, b = last[-2], last[-1]
                if a.boot_id == b.boot_id and b.uptime_ms > a.uptime_ms and b.out_total_l >= a.out_total_l:
                    draw = (b.out_total_l - a.out_total_l) / ((b.uptime_ms - a.uptime_ms) / 60000.0)
            night = self._power_night(hid, now)
            active = self._active_hunt(hid, now)
            lines = [night["text"]]
            if stale:
                lines.append("I can't see live data from your energy meter right now.")
            elif draw is not None:
                lines.append(f"Right now your home is drawing {_watts(draw)}.")
            topo = self._topology(hid)
            lat = [l for l in self.store.latches(hid) if l["pipe"] in topo.nodes]
            if lat:
                lines.append(f"I'm keeping {join_or(['the ' + topo.label(l['pipe']) for l in lat], 'and')} off, as you asked.")
            suggest = night["latest_w"] is not None and night["latest_w"] >= self.cfg.floor_lpm and not active and not lat
            if active:
                lines.append("You have a power hunt in progress; I can pick it up where we left off.")
            elif suggest:
                lines.append("Want me to find what's drawing it?" if self.control_available(hid) else
                             "Want me to walk you through finding it, one plug at a time?")
            return dict(kind="status", utility="power", simulated=src.simulated, time_scale=self.time_scale,
                        floor_lpm=self.cfg.floor_lpm, household=h["name"], speech=" ".join(lines),
                        flow_now_lpm=None if draw is None else round(draw, 3), draw_now_w=None if draw is None else round(draw, 1),
                        data_age_s=None if age is None else round(age, 1), stale=stale, night=night, active_hunt=active,
                        suggest_hunt=bool(suggest), source_error=err, control_available=self.control_available(hid),
                        latched=[l["pipe"] for l in lat])

    def night_flow(self, hid: str) -> dict:
        with self._lock:
            now = self.clock()
            self._poll(hid, now)
            n = self._power_night(hid, now)
            return dict(kind="night_flow", utility="power", simulated=self._source(hid).simulated, time_scale=self.time_scale,
                        floor_lpm=self.cfg.floor_lpm, speech=n["text"], **n)

    # ---------------------------------------------------------------- hunts: the same service, in power words
    def _present(self, hunt_id: str, now: float) -> dict:
        p = super()._present(hunt_id, now)
        if p.get("outcome") in ("partial", "upstream_remainder"):
            hunt = self.store.hunt(hunt_id)
            guarded = self._protected_labels(hunt["hid"])
            keep = (f" That includes the {join_or(guarded, 'and')}, which I never switch, and anything wired in." if guarded
                    else " That includes anything wired in.")
            if p["outcome"] == "partial":
                p["speech"] = p["speech"].replace(
                    "I found a confirmed source, but some flow remains that I can't place at any valve you've told me about.",
                    "The rest of the always-on power isn't behind any plug I can switch." + keep)
            else:
                p["speech"] = _upstream_power(p["speech"], keep)
        return p

    @_translated
    def start_hunt(self, hid: str, **kw) -> dict:
        p = super().start_hunt(hid, **kw)
        guarded = self._protected_labels(hid)
        if guarded and p.get("kind") in ("choose_mode", "needs_consent"):
            add = f" I never switch the {join_or(guarded, 'or')}."
            p = dict(p, speech=p["speech"] + add, consent_text=p.get("consent_text", "") + add)
        return p

    list_pipes = _translated(HuntService.list_pipes)
    set_pipe = _translated(HuntService.set_pipe)
    report = _translated(HuntService.report)
    stop_hunt = _translated(HuntService.stop_hunt)
    hunt_status = _translated(HuntService.hunt_status)
    water_status = _translated(water_status)
    night_flow = _translated(night_flow)

    def house_mode(self, hid: str, mode: str = "", confirm: bool = False) -> dict:
        raise UserError("House modes switch pipes. For plugs, ask me to switch a plug by name.")

    def start_fill(self, hid: str, outlet: str = "", litres: Optional[float] = None) -> dict:
        raise UserError("Filling is for taps, not plugs.")

    def evidence_html(self, packet: dict) -> str:
        return vocab.translate_html(super().evidence_html(packet))


def _upstream_power(speech: str, keep: str) -> str:
    """The water ending says the leak is upstream of every valve. For power: none of the plugs carries a draw it could see."""
    import re
    m = re.search(r"None of the valves you told me about stopped the flow \((.*?) still moving\)\..*?evidence\.", speech)
    if not m:
        return speech
    return speech.replace(m.group(0), f"None of the plugs I can switch carries the always-on power ({m.group(1)} still drawn)."
                                      + keep)
