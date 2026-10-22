"""Fill by volume: "Alexa, fill the bath, 120 litres", "fill the pot, 3 litres", "water the plants".

The same flow meter and the same pipe controller that run a leak hunt also do the everyday job of filling a bath or a
bucket, without anyone walking to the tap. Voice-controlled filling exists in other products; what is particular here:

  * The controller's timer IS the fill timer. An outlet (a tap valve: bath, kitchen tap, garden hose) rests CLOSED and opens only on a lease. Alexa
    keeps that lease equal to the time the remaining litres need at the measured rate, so if Alexa dies mid-fill the
    controller still closes the tap at about the target. Without fresh meter data she stops extending it.
  * The fill counts only the outlet's water: the flow that was already moving before the tap opened is measured and
    left out. When the flow then changes in a way the tap does not explain (someone else opened or closed a tap), the
    meter is no longer measuring only the bath, so the fill stops instead of over-filling. Small changes below the
    detection threshold can go unnoticed; docs/HONEST_UNKNOWNS.md gives the bound.
  * Measured twice. A change in the seconds before the tap opened (someone turned a tap off just before asking) sits
    inside the baseline, where no window in the fill can see it, and would be counted wrongly for the whole fill. So a
    fill expected to run 90 s or more pauses the tap once, about a third of the way in, re-measures what is moving
    elsewhere over two agreeing meter intervals, redoes the count, and carries on; a house that changes during the pause
    ends the fill with the tap closed. After the pause the tap's own corrected flow is the reference for change detection.
  * If the outlet valve does not close, Alexa closes the branch valve above it (when the controller has one).
  * Water that kept moving with the tap closed is reported at the end, with an offer to find where it goes (a leak hunt).

Everything here is policy in the add-on. The engine (huntkit) is not involved, and the demo's outlet is simulated.
"""
from __future__ import annotations

import os
import secrets
from typing import List, Optional

from .actuators import CLOSED, MAX_OUTLET_LEASE_S, OPEN

FIRST_LEASE_S = 30.0          # the first opening, before any reading has measured the rate
MIN_LEASE_S = 1.0
SETTLE_S = 2.0                # flow right after the valve opens is not used to measure the rate
CHANGE_WINDOWS = (1, 3, 6, 12)  # the mean of the last k intervals is compared with the first two, for each k: a persistent change
                                #   shows up in a longer window even when one interval's noise would hide it
DONE_TOL_L = 0.5              # close when this close to the target
RENEW_DELTA_S = 3.0           # re-lease when the controller's timer is off by more than this
MAX_TIME_FACTOR = 3.0         # a fill taking this many times its nominal duration is stopped
NO_FLOW_LPM = 0.5             # less than this through an open outlet, two intervals in a row: it isn't getting water
STALE_END_S = 60.0            # no meter data this long after the tap closed: end without a final count
HUB_GRACE_S = 15.0
RECHECK_MIN_FILL_S = 90.0     # fills expected to run at least this long pause once to re-measure the house's other water use
RECHECK_AT = 0.3              #   ...when this share of the expected time has passed (and at least RECHECK_AFTER_S)
RECHECK_AFTER_S = 40.0
RECHECK_MAX_S = 60.0          # a re-check that gets no clean meter interval in this long ends the fill with the tap closed

MUTANT = os.environ.get("FILL_MUTANT", "")   # deliberately broken variants for tools/fill_fuzz.py; never set in production


class FillError(Exception):
    pass


def _l(x: float) -> str:
    return f"{x:.0f}"


def _lpm(x: float) -> str:
    return f"{x:.1f} litres a minute"


class Filler:
    def __init__(self, svc):
        self.svc = svc

    # ------------------------------------------------------------------ configuration
    def outlets(self, hid: str) -> List[dict]:
        h = self.svc.store.household(hid)
        if not h:
            return []
        hub = self.svc._hub(hid)
        if hub is None:
            return []
        return [dict(o) for o in (h["source"].get("outlets") or []) if hub.has(o["id"])]

    def active(self, hid: str) -> Optional[dict]:
        f = self.svc.store.latest_fill(hid)
        return f if f and f["ended"] is None else None

    def resolve(self, hid: str, name: str) -> dict:
        outs = self.outlets(hid)
        if not outs:
            raise FillError("There's no tap here that I can fill from. Filling needs an outlet valve on your pipe controller.")
        q = " ".join((name or "").lower().replace("the ", " ").split())
        if not q:
            if len(outs) == 1:
                return outs[0]
            raise FillError("Which one should I fill: " + " or ".join("the " + o["label"] for o in outs) + "?")
        hits = []
        for o in outs:
            names = {o["id"].lower(), o["label"].lower()} | {a.lower() for a in o.get("aliases", [])}
            if q in names or any(q == n or (len(q) >= 3 and (q in n.split() or n in q.split() or n in q)) for n in names):
                hits.append(o)
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise FillError(f"I don't know a tap called {name}. I can fill " + " or ".join("the " + o["label"] for o in outs) + ".")
        raise FillError("Which one: " + " or ".join("the " + o["label"] for o in hits) + "?")

    # ------------------------------------------------------------------ meter
    def _readings(self, hid: str, since: float):
        return self.svc.store.readings(hid, since=since)

    def _baseline(self, hid: str, now: float):
        """Flow over the last measured interval before the tap opens (litres a minute), and the reference reading."""
        rs = self._readings(hid, now - 120)
        rs = [r for r in rs if r.t <= now]
        if len(rs) < 2 or now - rs[-1].t > self.svc.cfg.stale_s:
            raise FillError("I can't see your water meter right now, so I won't open a tap I can't measure.")
        a, b = rs[-2], rs[-1]
        if a.boot_id != b.boot_id or b.t <= a.t:
            raise FillError("Your water meter has just restarted. Give it a few seconds and ask me again.")
        if b.valve != "OPEN" or b.leak:
            raise FillError("Your main safety valve is closed, so there's no water to fill with. It is re-armed only at the device.")
        base = max(0.0, (b.out_total_l - a.out_total_l) / (b.t - a.t) * 60.0)
        return base, b

    # ------------------------------------------------------------------ start / stop / status
    def start(self, hid: str, outlet: str = "", litres: Optional[float] = None) -> dict:
        svc = self.svc
        with svc._lock:
            now = svc.clock()
            hub = svc._hub(hid)
            if hub is None:
                raise FillError("I can't switch anything in this home, so I can't fill from a tap.")
            o = self.resolve(hid, outlet)
            if self.active(hid):
                f = self.active(hid)
                raise FillError(f"I'm already filling the {f['state']['label']}. Say 'stop filling' first if you want to change it.")
            if svc._active_hunt(hid, now):
                raise FillError("A leak hunt is running, and filling would disturb its measurements. "
                                "I'll fill it when the hunt is done, or say 'stop the test' first.")
            target = float(o.get("preset_l") or 0) if litres is None else float(litres)
            max_l = float(o.get("max_l") or 0)
            if target <= 0:
                raise FillError(f"How many litres should I put in the {o['label']}?")
            if max_l and target > max_l:
                raise FillError(f"I fill the {o['label']} with at most {_l(max_l)} litres. Ask for {_l(max_l)} or less.")
            topo = svc._topology(hid)
            parent = o.get("parent")
            blocked = [p for p in ([parent] + (topo.ancestors(parent) if parent in topo.nodes else []))
                       if p in topo.nodes and p in {l["pipe"] for l in svc.store.latches(hid)}]
            if blocked:
                raise FillError(f"The {topo.label(blocked[0])} is being kept closed after a leak, so the {o['label']} has no water. "
                                f"Once it's repaired, say 'turn on the {topo.label(blocked[0])}'.")
            offers = [x for x in svc.store.offers(hid, now) if x["pipe"] in topo.nodes]
            if offers:                                               # its lease would reopen that pipe in the middle of the fill
                pl = topo.label(offers[0]["pipe"])
                raise FillError(f"I'm holding the {pl} off while you decide whether to keep it closed, and it would come back on in the "
                                f"middle of the fill. Answer that first: say 'keep it off' or 'turn it back on'.")
            try:
                pos = hub.pipes(now)
            except Exception:
                raise FillError("I can't reach your pipe controller right now, so I won't open a tap.")
            upstream = [p for p in ([parent] + (topo.ancestors(parent) if parent in topo.nodes else []))
                        if p in topo.nodes and pos.get(p) is not None and pos[p].position == CLOSED]
            if upstream:
                pl = topo.label(upstream[0])
                hm = svc.store.house_mode(hid)
                if hm and upstream[0] in hm["state"].get("closed", []):
                    back = {"sleep": "I'm up", "away": "I'm home"}.get(hm["mode"], "I'm home")
                    raise FillError(f"{hm['mode'].capitalize()} mode has the {pl} off, so the {o['label']} has no water. "
                                    f"Say '{back}' first, or 'turn on the {pl}'.")
                raise FillError(f"The {pl} is off, so the {o['label']} has no water. Say 'turn on the {pl}' first.")
            svc._poll(hid, now)
            base, ref = self._baseline(hid, now)
            nominal = float(o.get("nominal_lpm") or 12.0)
            lease0 = max(MIN_LEASE_S, min(FIRST_LEASE_S, target / nominal * 60.0))
            if MUTANT == "long_lease":
                lease0 = MAX_OUTLET_LEASE_S
            ack = svc._command(hid, None, o["id"], OPEN, "fill", lease0, force=True)
            if not ack.ok:
                why = {"stuck": "didn't move", "hub_unreachable": "can't be reached", "rate_limited": "was only just switched"}.get(ack.error, f"didn't respond ({ack.error})")
                raise FillError(f"The {o['label']} valve {why}, so I haven't started filling.")
            fid = "fill-" + secrets.token_hex(6)
            state = dict(label=o["label"], vessel=bool(o.get("vessel")), parent=parent, target_l=target, nominal_lpm=nominal, baseline_lpm=round(base, 3),
                         t_ref=ref.t, total_ref=ref.out_total_l, boot_ref=ref.boot_id, t_open=ack.t_ack, phase="filling",
                         lease_until=ack.t_ack + lease0, rate_lpm=None, delivered_l=0.0, last_t=ref.t, renewals=0, paused=False,
                         t_closed=None, close_reason=None, backup=None, hub_down_since=None,
                         offer_hunt=base >= svc.cfg.floor_lpm)
            svc.store.create_fill(fid, hid, o["id"], target, now, state)
            return self.present(fid, now)

    def stop(self, hid: str) -> dict:
        svc = self.svc
        with svc._lock:
            now = svc.clock()
            f = self.active(hid)
            if not f:
                raise FillError("Nothing is filling right now.")
            st = f["state"]
            if st["phase"] in ("filling", "recheck"):
                self._close(f, now, "stopped")
            return self.present(f["id"], now)

    def status(self, hid: str, fill_id: Optional[str] = None) -> dict:
        svc = self.svc
        with svc._lock:
            now = svc.clock()
            f = svc.store.fill(fill_id) if fill_id else svc.store.latest_fill(hid)
            if not f or f["hid"] != hid:
                raise FillError("I haven't filled anything yet. Say 'fill the bath' to start.")
            svc._poll(hid, now, min_gap=3.0)
            if f["ended"] is None:
                self._drive(f, now)
            return self.present(f["id"], now)

    # ------------------------------------------------------------------ the runner
    def tick(self, now: float) -> None:
        for f in self.svc.store.open_fills():
            try:
                self.svc._poll(f["hid"], now, min_gap=3.0)
                self._drive(self.svc.store.fill(f["id"]), now)
            except FillError:
                pass

    def _measure(self, f: dict, now: float):
        """(delivered litres at the last reading, last reading, interval rates of the outlet after it settled)."""
        st = f["state"]
        rs = [r for r in self._readings(f["hid"], st["t_ref"]) if r.t >= st["t_ref"]]
        restarted = any(r.boot_id != st["boot_ref"] for r in rs)
        rs = [r for r in rs if r.boot_id == st["boot_ref"]]
        last = rs[-1] if rs else None
        base = 0.0 if MUTANT == "no_baseline" else st["baseline_lpm"]
        d = 0.0 if last is None else (last.out_total_l - st["total_ref"]) - base * (last.t - st["t_ref"]) / 60.0
        end = st["t_closed"] if st["t_closed"] is not None else float("inf")
        rates = []
        for a, b in zip(rs, rs[1:]):
            if a.t >= st.get("t_rates_from", st["t_open"]) + SETTLE_S and b.t <= end and b.t > a.t and not st.get("t_paused"):
                rates.append((b.out_total_l - a.out_total_l) / (b.t - a.t) * 60.0 - base)
        return max(0.0, d), last, rates, restarted

    def threshold_lpm(self, total_lpm: float, k: int) -> float:
        """The smallest change in the mean of k intervals that the engine's own sensor model lets us call real: the same
        per-interval rule a hunt uses (stable_z standard deviations of rel_sd relative variability), averaged over k."""
        cfg = self.svc.cfg
        return max(cfg.min_effect_lpm, cfg.stable_z * cfg.rel_sd * max(total_lpm, 0.0) / (k ** 0.5))

    def _changed(self, rates: List[float], base: float, ref: Optional[float] = None) -> bool:
        """Did the house's other water use change? Windows of the tap's measured flow against a reference: the first two
        intervals after it opened, or after a re-check pause the tap's own flow measured before the pause (corrected by the
        re-measured baseline), so that a change just after the tap reopens cannot hide inside a fresh reference."""
        if MUTANT == "ignore_change":
            return False
        if ref is None:
            if len(rates) < 2:
                return False
            if len(rates) == 2:
                return abs(rates[1] - rates[0]) > self.threshold_lpm(base + rates[0], 1)
            ref = (rates[0] + rates[1]) / 2.0
            later = rates[2:]
        else:
            later = rates
        for k in CHANGE_WINDOWS:
            if len(later) >= k and abs(sum(later[-k:]) / k - ref) > self.threshold_lpm(base + ref, k):
                return True
        return False

    def _drive(self, f: dict, now: float) -> None:
        svc, st, hid, oid = self.svc, f["state"], f["hid"], f["outlet"]
        if f["ended"] is not None:
            return
        d, last, rates, restarted = self._measure(f, now)
        st["delivered_l"] = round(d, 3)
        if last is not None:
            st["last_t"] = last.t
        age = None if last is None else now - last.t
        stale = age is None or age > svc.cfg.stale_s
        if rates:
            st["rate_lpm"] = round(rates[0] if len(rates) == 1 else (rates[0] + rates[1]) / 2.0, 3)
        hub = svc._hub(hid)
        try:
            allpos = hub.pipes(now) if hub is not None else {}
            pos = allpos.get(oid)
            st["hub_down_since"] = None
        except Exception:
            allpos, pos = {}, None
            st["hub_down_since"] = st["hub_down_since"] or now
        topo = svc._topology(hid)
        par = st.get("parent")
        upstream_off = [p for p in ([par] + (topo.ancestors(par) if par in topo.nodes else [])) if p in topo.nodes and
                        allpos.get(p) is not None and allpos[p].position == CLOSED]
        was_settling = st["phase"] == "settling"                     # `pos` above was read before anything this tick moved the tap
        was_recheck = st["phase"] == "recheck"
        if st["phase"] == "filling":
            if pos is None:
                if st["hub_down_since"] is not None and now - st["hub_down_since"] > HUB_GRACE_S:
                    st["phase"], st["close_reason"], st["t_closed"] = "settling", "hub_unreachable", st.get("lease_until") or now
                svc.store.save_fill_state(f["id"], st)
                return
            if pos.position != OPEN:                                  # the controller's own timer closed it (or someone did)
                lu = st.get("lease_until")
                st["phase"] = "settling"
                st["seen_closed"] = pos.position == CLOSED
                st["t_closed"] = lu if lu is not None and lu <= now else now
                st["close_reason"] = "meter_lost" if stale else ("controller_timer" if lu is not None and lu <= now + 0.5 else "closed_elsewhere")
            elif pos.lease_left_s is None and (st.get("lease_until") or now) < now - 0.5:
                self._close(f, now, "outlet_stuck")                    # its lease ran out and it is still open: jammed
            elif restarted:
                self._close(f, now, "meter_restarted")
            elif upstream_off:
                st["upstream_off"] = upstream_off[0]
                self._close(f, now, "branch_closed")
            elif self._changed(rates, st["baseline_lpm"], st.get("ref_tap_lpm")):
                self._close(f, now, "flow_changed")
            elif len(rates) >= 2 and rates[-1] < NO_FLOW_LPM and rates[-2] < NO_FLOW_LPM:
                self._close(f, now, "no_flow")
            elif now - st["t_open"] > max(120.0, st["target_l"] / st["nominal_lpm"] * 60.0 * MAX_TIME_FACTOR):
                self._close(f, now, "too_slow")
            elif stale and MUTANT != "renew_stale":
                st["paused"] = True                                    # no new lease: the controller closes it when this one ends
            elif self._recheck_due(st, now, d):
                self._start_recheck(f, now)
            else:
                st["paused"] = False
                if st["rate_lpm"]:
                    r = st["rate_lpm"]
                elif last is not None and last.t - st["t_open"] >= 5.0 and d > 0:
                    r = d * 60.0 / (last.t - st["t_open"])
                else:
                    r = st["nominal_lpm"]
                r = max(r, 0.1)
                since = (now - last.t) if last is not None else 0.0
                remaining = st["target_l"] - d - r * since / 60.0
                if remaining <= DONE_TOL_L:
                    self._close(f, now, "target")
                else:
                    want = min(MAX_OUTLET_LEASE_S, max(MIN_LEASE_S, remaining / r * 60.0))
                    if MUTANT == "long_lease":
                        want = MAX_OUTLET_LEASE_S
                    left = pos.lease_left_s
                    if left is None or abs(left - want) > RENEW_DELTA_S:
                        ack = svc._command(hid, None, oid, OPEN, "fill-renew", want, force=True, audit=False)
                        if ack.ok:
                            st["lease_until"] = now + want
                            st["renewals"] += 1
                        elif ack.error == "stuck":
                            self._close(f, now, "outlet_stuck")
        if was_recheck:
            self._recheck(f, now, pos, last, restarted, stale)
            if st["phase"] == "recheck" or f["ended"] is not None:
                svc.store.save_fill_state(f["id"], st)
                return
        if was_settling and pos is not None:
            if pos.position == CLOSED:
                st["seen_closed"] = True
            elif pos.position == OPEN and st.get("seen_closed") and st["close_reason"] != "outlet_stuck":
                self._close(f, now, "outlet_stuck")                    # it was closed and now reports open again: treat it as jammed
        if st["phase"] == "settling":
            after = last is not None and st["t_closed"] is not None and last.t >= st["t_closed"] + 0.5
            if after or (age is not None and age > STALE_END_S) or st["close_reason"] in ("hub_unreachable",) or \
                    (st["close_reason"] == "outlet_stuck" and not st.get("backup")):
                outcome = {"target": "done", "controller_timer": "done"}.get(st["close_reason"], st["close_reason"])
                if not after and outcome == "done":
                    outcome = "meter_lost"
                st["phase"] = "ended"
                svc.store.end_fill(f["id"], now, outcome, st)
                return
        svc.store.save_fill_state(f["id"], st)

    # ------------------------------------------------------------------ the mid-fill re-check (measure twice)
    def _recheck_due(self, st: dict, now: float, d: float) -> bool:
        """A long fill pauses once to re-measure the other water use. The count subtracts the flow measured before the tap
        opened; if that changed just before (someone turned a tap off a few seconds before asking), no window inside the fill
        can see it, and the count would be off for the whole fill. Closing the tap for two meter intervals measures it again."""
        if MUTANT == "no_recheck" or st.get("rechecked"):
            return False
        expected = st["target_l"] / st["nominal_lpm"] * 60.0
        if expected < RECHECK_MIN_FILL_S:
            return False
        r = st.get("rate_lpm") or st["nominal_lpm"]
        left_s = (st["target_l"] - d) / max(r, 0.1) * 60.0
        return now - st["t_open"] >= max(RECHECK_AFTER_S, RECHECK_AT * expected) and left_s > 30.0

    def _start_recheck(self, f: dict, now: float) -> None:
        svc, st, hid, oid = self.svc, f["state"], f["hid"], f["outlet"]
        ack = svc._command(hid, None, oid, CLOSED, "fill-recheck", None, force=True)
        if ack.ok:
            st["phase"], st["rechecked"], st["t_paused"], st["t_recheck"] = "recheck", True, ack.t_ack, ack.t_ack
            st["seen_closed"] = True
        elif ack.error == "stuck":
            self._close(f, now, "outlet_stuck")
        # unreachable: try again next tick; the controller's lease still bounds the tap

    def _recheck(self, f: dict, now: float, pos, last, restarted: bool, stale: bool) -> None:
        svc, st, hid, oid = self.svc, f["state"], f["hid"], f["outlet"]
        t0 = st["t_paused"]
        if pos is not None and pos.position == OPEN:                   # closed for the re-check, and reports open: jammed
            self._close(f, now, "outlet_stuck")
            return
        if restarted:
            st["phase"], st["close_reason"], st["t_closed"], st["t_paused"] = "settling", "meter_restarted", t0, None
            return
        rs = [r for r in self._readings(hid, t0) if r.t >= t0 + SETTLE_S and r.boot_id == st["boot_ref"]]
        if len(rs) < 3:
            if now - t0 > RECHECK_MAX_S:                               # no clean intervals: stop here, the tap is already closed
                st["phase"], st["close_reason"], st["t_closed"], st["t_paused"] = "settling", "meter_lost_paused", t0, None
            return
        r1, r2 = [max(0.0, (y.out_total_l - x.out_total_l) / (y.t - x.t) * 60.0) for x, y in ((rs[0], rs[1]), (rs[1], rs[2]))]
        if abs(r2 - r1) > self.threshold_lpm(max(r1, r2), 1):        # the house changed while I was measuring it: don't resume on it
            st["phase"], st["close_reason"], st["t_closed"], st["t_paused"] = "settling", "flow_changed", t0, None
            return
        other = (r1 + r2) / 2.0
        before = st["baseline_lpm"]
        st["baseline_before_lpm"] = before
        st["baseline_lpm"] = round(other, 3)                            # the count is redone with what is really moving elsewhere
        if st.get("rate_lpm"):                                          # the rate so far had the old baseline subtracted
            st["rate_lpm"] = round(max(0.1, st["rate_lpm"] + before - other), 3)
            st["ref_tap_lpm"] = st["rate_lpm"]                          # the tap's own flow: the reference after it reopens
        st["offer_hunt"] = other >= svc.cfg.floor_lpm
        st["t_paused"] = None
        d, last2, _, _ = self._measure(f, now)
        st["delivered_l"] = round(d, 3)
        remaining = st["target_l"] - d
        if remaining <= DONE_TOL_L:
            st["phase"], st["close_reason"], st["t_closed"] = "settling", "target", t0
            return
        r = st.get("rate_lpm") or st["nominal_lpm"]
        want = min(MAX_OUTLET_LEASE_S, max(MIN_LEASE_S, remaining / max(r, 0.1) * 60.0))
        ack = svc._command(hid, None, oid, OPEN, "fill-resume", want, force=True)
        if ack.ok:
            st["phase"], st["t_rates_from"], st["lease_until"] = "filling", ack.t_ack, ack.t_ack + want
            st["resumed_at"] = ack.t_ack
        elif ack.error == "stuck":                                      # it will not open again: nothing is running, say so
            st["phase"], st["close_reason"], st["t_closed"] = "settling", "resume_failed", t0
        else:                                                           # unreachable with the tap closed: end, nothing is running
            st["phase"], st["close_reason"], st["t_closed"] = "settling", "hub_unreachable_paused", t0

    def _close(self, f: dict, now: float, reason: str) -> None:
        svc, st, hid, oid = self.svc, f["state"], f["hid"], f["outlet"]
        if MUTANT == "no_stop_close" and reason == "stopped":
            st["phase"], st["close_reason"], st["t_closed"] = "settling", reason, now
            svc.store.save_fill_state(f["id"], st)
            return
        ack = svc._command(hid, None, oid, CLOSED, "fill", None, force=True)
        jammed = (not ack.ok and ack.error == "stuck") or reason == "outlet_stuck"
        if ack.ok and reason != "outlet_stuck":
            st["phase"], st["close_reason"], st["t_closed"] = "settling", reason, ack.t_ack
            st["seen_closed"] = True
        elif jammed:
            st["close_reason"], st["phase"] = "outlet_stuck", "settling"
            parent = st.get("parent")
            topo = svc._topology(hid)
            if MUTANT != "no_backup" and parent in topo.nodes and parent in svc._actuated(hid, topo):
                b = svc._command(hid, None, parent, CLOSED, "fill-backup", None, force=True)
                if b.ok:
                    st["backup"] = parent
                    st["t_closed"] = b.t_ack
            if not st.get("backup"):
                st["t_closed"] = now
        else:                                                         # the controller can't be reached: its own lease will close it
            st["hub_down_since"] = st["hub_down_since"] or now
            if now - st["hub_down_since"] > HUB_GRACE_S:
                st["phase"], st["close_reason"], st["t_closed"] = "settling", "hub_unreachable", st.get("lease_until") or now
        svc.store.save_fill_state(f["id"], st)

    # ------------------------------------------------------------------ what Alexa says and the screen shows
    def _trace(self, f: dict, now: float) -> dict:
        """Recent flow as stepped intervals, with the moments the tap opened and closed, for the screen's sparkline."""
        st = f["state"]
        end = now if f["ended"] is None else min(now, f["ended"] + 30.0)
        rs = self.svc.store.readings(f["hid"], since=max(st["t_ref"] - 30.0, end - 1200.0), until=end)   # the whole fill, and the flow before it
        pts = []
        for a, b in zip(rs, rs[1:]):
            if a.boot_id != b.boot_id or b.uptime_ms <= a.uptime_ms or b.out_total_l < a.out_total_l:
                continue
            pts.append([round(a.t - end, 1), round(b.t - end, 1), round((b.out_total_l - a.out_total_l) / ((b.uptime_ms - a.uptime_ms) / 60000.0), 3)])
        span = float(min(1200.0, max(60.0, -(pts[0][0] if pts else 0.0))))
        tap = st["label"] if any(w in st["label"] for w in ("tap", "hose")) else st["label"] + " tap"
        marks = [dict(t=round(t - end, 1), label=lab) for t, lab in ((st["t_open"], tap + " opened"),
                                                                     (st.get("t_recheck"), "paused to re-check"),
                                                                     (st.get("t_closed"), tap + " closed"))
                 if t is not None and -span <= t - end <= 0]
        return dict(span_s=span, points=pts, marks=marks, floor_lpm=self.svc.cfg.floor_lpm)

    def present(self, fill_id: str, now: float) -> dict:
        svc = self.svc
        f = svc.store.fill(fill_id)
        st, hid = f["state"], f["hid"]
        topo = svc._topology(hid)
        label, target, d = st["label"], st["target_l"], st["delivered_l"]
        vessel = bool(st.get("vessel"))                     # a bath is filled; a tap or a hose runs a number of litres
        base = st["baseline_lpm"]
        stopped_at = f"I stopped filling the {label}" if vessel else f"I closed the {label}"
        lease_left = None
        try:
            hub = svc._hub(hid)
            p = hub.pipes(now).get(f["outlet"]) if hub is not None else None
            lease_left = None if p is None or p.lease_left_s is None else round(p.lease_left_s, 1)
        except Exception:
            pass
        r = st.get("rate_lpm")
        eta = None if not r or f["ended"] is not None else max(0.0, (target - d) / r * 60.0)
        outcome = f["outcome"]
        b0 = st.get("baseline_before_lpm")
        corrected = b0 is not None and abs(b0 - base) >= svc.cfg.min_effect_lpm
        note = (f" Part way through I paused the tap to re-measure the water moving elsewhere: it had changed from about "
                f"{_lpm(b0)} to {_lpm(base)}, so I corrected the count.") if corrected else ""
        bridge = note + ((f" Before I opened the tap, about {_lpm(base)} was already moving somewhere else. " if not corrected else
                          f" About {_lpm(base)} is still moving somewhere else. ")
                         + "If nothing should be running, I can find where it's going. Want me to?" if st["offer_hunt"] else "")
        if f["ended"] is None:
            if st["phase"] == "recheck":
                speech = (f"Pausing the {label} for a few seconds to re-check the count: about {_l(d)} of {_l(target)} litres so far. "
                          f"It carries on by itself.")
            elif st["phase"] == "settling":
                speech = f"I've closed the {label}. Checking the last reading for the final count."
            elif st["paused"]:
                speech = (f"I've lost my water-meter data, so I'm not extending the {label}. Your pipe controller closes it by itself"
                          + (f" within {int(lease_left) + 1} seconds." if lease_left is not None else " when its timer runs out."))
            elif d < 0.5 and not r:
                speech = ((f"Filling the {label} with {_l(target)} litres." if vessel else f"Running {_l(target)} litres from the {label}.")
                          + f" I'll stop it at {_l(target)}, and your pipe controller closes it by itself if it stops hearing from me.")
                if st["offer_hunt"]:
                    speech += f" About {_lpm(base)} was already moving somewhere else before I opened it; I'm leaving that out of the count."
            else:
                speech = f"{'Filling' if vessel else 'Running'} the {label}: about {_l(d)} of {_l(target)} litres" + (f", about {int(round(eta))} seconds to go." if eta is not None else ".")
        elif outcome == "done":
            speech = (f"The {label} is filled: about {_l(d)} litres went in." if vessel
                      else f"About {_l(d)} litres came out of the {label}.") + bridge
        elif outcome == "stopped":
            speech = (f"Stopped. The {label} has about {_l(d)} litres." if vessel
                      else f"Stopped. About {_l(d)} litres came out of the {label}.") + bridge
        elif outcome == "flow_changed":
            speech = (f"{stopped_at} at about {_l(d)} litres. The water use in the house changed in the middle, so my meter "
                      f"wasn't measuring only the {label} any more, and I'd rather stop than "
                      + ("overfill it." if vessel else "give you more than you asked for."))
        elif outcome == "meter_lost":
            speech = (f"I lost my water-meter data, so your pipe controller closed the {label} on its own timer. About {_l(d)} litres had "
                      f"gone in when I last saw the meter")
            if r and st.get("t_closed") and st.get("last_t") and st["t_closed"] > st["last_t"]:
                speech += f"; at the rate it was filling, about {_l(d + r * (st['t_closed'] - st['last_t']) / 60.0)} by the time it closed."
            else:
                speech += "."
        elif outcome == "resume_failed":
            speech = (f"I paused the {label} to re-check the count and its valve didn't open again, so it stays closed. "
                      f"About {_l(d)} litres went in.")
        elif outcome == "hub_unreachable_paused":
            speech = (f"I paused the {label} to re-check the count and then couldn't reach your pipe controller to start it again, "
                      f"so it stays closed. About {_l(d)} litres went in.")
        elif outcome == "meter_lost_paused":
            speech = (f"I paused the {label} to re-check the count and then lost my water-meter data, so I've left it closed. "
                      f"About {_l(d)} litres went in.")
        elif outcome == "outlet_stuck":
            if st.get("backup"):
                pl = topo.label(st["backup"])
                speech = (f"The {label} valve didn't close (it still reports open), so I closed the {pl} above it. It stays closed until the "
                          f"valve is fixed; then say "
                          f"'turn on the {pl}'. About {_l(d)} litres went in.")
            else:
                speech = (f"The {label} valve didn't close and I can't close anything above it. Turn the water off at its shut-off or at "
                          f"the main now.")
        elif outcome == "no_flow":
            speech = f"The {label} isn't getting water, so I closed it again. Check that its supply is turned on."
        elif outcome == "too_slow":
            speech = f"The {label} was filling much more slowly than it should, so I stopped at about {_l(d)} litres."
        elif outcome == "hub_unreachable":
            speech = (f"I can't reach your pipe controller. It closes the {label} by itself when its timer runs out, within "
                      f"{int(MAX_OUTLET_LEASE_S)} seconds at the latest.")
        elif outcome == "meter_restarted":
            speech = f"Your water meter restarted in the middle, so I stopped at about {_l(d)} litres rather than guess."
        elif outcome == "branch_closed":
            speech = (f"The {topo.label(st.get('upstream_off', st.get('parent')))} was turned off, so the {label} stopped getting water. "
                      f"I closed its tap at about {_l(d)} litres.")
        elif outcome == "closed_elsewhere":
            speech = f"The {label} was closed by something else, so I stopped at about {_l(d)} litres."
        else:
            speech = f"{stopped_at} at about {_l(d)} litres."
        return dict(kind="fill", fill_id=f["id"], status="active" if f["ended"] is None else "done", outcome=outcome,
                    phase=st["phase"], outlet=f["outlet"], label=label, vessel=vessel, target_l=target, delivered_l=round(d, 1),
                    rate_lpm=None if not r else round(r, 2), eta_s=None if eta is None else round(eta, 1), lease_left_s=lease_left,
                    baseline_lpm=round(base, 2), offer_hunt=bool(st["offer_hunt"]) and f["ended"] is not None and outcome in ("done", "stopped"),
                    paused=bool(st["paused"]), backup=st.get("backup"), speech=speech, trace=self._trace(f, now),
                    simulated=bool(getattr(svc._source(hid), "simulated", False)), time_scale=svc.time_scale)
