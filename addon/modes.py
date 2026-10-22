"""House modes: "Alexa, sleep mode", "away mode", "I'm up" / "I'm home".

A mode switches off, after a spoken yes, every pipe the controller can switch except protected ones and the ones the mode keeps on
(sleep mode keeps the bathroom, so nobody wakes up to a dry toilet). Then it measures: the first full meter interval after the last
valve closed says whether water is still moving, and where it can be. That check is what a routine that fires valves cannot do.

"I'm up" / "I'm home" turns back on exactly the pipes the mode turned off. A pipe that was already off before the mode, or that is
kept closed after a leak, stays as it was. A mode is the customer's lasting decision, like a spoken "off", so its valves are closed
without a lease and stay closed if the add-on goes away; the controller never reopens them on its own.

A mode does not start while a leak hunt or a fill is running, or while a "keep it closed?" question is open (that pipe's lease
would reopen it under the mode). Everything here is policy in the add-on; the controller and the house are simulated in the demo.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from huntkit.engine import fmt_lpm
from huntkit.pipes import join_or

from .actuators import CLOSED, OPEN

SETTLE_S = 2.0                       # flow right after a valve moves is not used for the check
MODES: Dict[str, dict] = {
    "sleep": dict(label="sleep mode", keep_rooms=("bathroom",), back="I'm up"),
    "away": dict(label="away mode", keep_rooms=(), back="I'm home"),
}
SYNONYMS = {
    "sleep": "sleep", "sleep mode": "sleep", "night": "sleep", "night mode": "sleep", "bed": "sleep", "bedtime": "sleep",
    "good night": "sleep", "goodnight": "sleep",
    "away": "away", "away mode": "away", "holiday": "away", "vacation": "away", "work": "away", "work mode": "away",
    "leaving": "away", "out": "away", "going out": "away",
    "home": "home", "i'm home": "home", "im home": "home", "back": "home", "i'm back": "home", "up": "home", "i'm up": "home",
    "im up": "home", "morning": "home", "good morning": "home", "wake": "home", "awake": "home", "off": "home",
    "normal": "home", "home mode": "home",
}
MUTANT = ""                          # deliberately broken variants for tests/test_modes.py; never set in production


class ModeError(Exception):
    pass


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def resolve_mode(word: str) -> Optional[str]:
    q = " ".join((word or "").lower().replace("’", "'").replace("mode", " mode").split())
    if q in SYNONYMS:
        return SYNONYMS[q]
    q2 = q.replace(" mode", "").strip()
    return SYNONYMS.get(q2)


class Modes:
    def __init__(self, svc):
        self.svc = svc

    # ------------------------------------------------------------------ the plan
    def plan(self, hid: str, mode: str) -> dict:
        """Which pipes the mode switches off, which it keeps on, and which it never touches."""
        svc = self.svc
        topo = svc._topology(hid)
        acts = set(svc._actuated(hid, topo))
        keep_rooms = MODES[mode]["keep_rooms"]
        keep = {i for i, n in topo.nodes.items() if n.room in keep_rooms}
        for i in list(keep):                                          # the pipes feeding a kept pipe must stay on too
            keep.update(a for a in topo.ancestors(i) if a in topo.nodes)
        protected = [i for i, n in topo.nodes.items() if n.protect]
        cand = [i for i in topo.nodes if i in acts and i not in keep and i not in protected]
        top = [i for i in cand if not any(a in cand for a in topo.ancestors(i))]   # closing a branch closes what hangs below it
        covered = sorted({i for i in cand} | {c for i in top for c in self._below(topo, i)})
        hand = [i for i in topo.nodes if i not in acts and i not in protected and i not in keep
                and not any(a in top for a in topo.ancestors(i))]
        return dict(close=top, covered=covered, keep=sorted(k for k in keep if k in topo.nodes), protected=protected, hand=hand)

    def _below(self, topo, i: str) -> List[str]:
        out, stack = [], list(topo.children(i))
        while stack:
            c = stack.pop()
            out.append(c)
            stack.extend(topo.children(c))
        return out

    # ------------------------------------------------------------------ set / status
    def set(self, hid: str, word: str, confirm: bool = False) -> dict:
        svc = self.svc
        with svc._lock:
            now = svc.clock()
            mode = resolve_mode(word) if word else None
            if word and mode is None:
                raise ModeError("I know sleep mode and away mode. Say 'I'm home' to turn the pipes back on.")
            if not word:
                return self.status(hid)
            if mode == "home":
                return self._home(hid, now)
            hub = svc._hub(hid)
            if hub is None:
                raise ModeError("I can't switch pipes in this home, so I can't put it in a mode.")
            cur = svc.store.house_mode(hid)
            if cur and cur["mode"] == mode:
                return self.status(hid)
            if svc._active_hunt(hid, now):
                raise ModeError("A leak hunt is running. Say 'stop the test' first, then ask me again.")
            if svc.filler.active(hid):
                raise ModeError(f"I'm filling the {svc.filler.active(hid)['state']['label']}. Say 'stop filling' first, or ask "
                                "me again when it's done.")
            topo = svc._topology(hid)
            offers = [o for o in svc.store.offers(hid, now) if o["pipe"] in topo.nodes]
            if offers:
                pl = topo.label(offers[0]["pipe"])
                raise ModeError(f"I'm holding the {pl} off while you decide whether to keep it closed. Answer that first: say "
                                "'keep it off' or 'turn it back on'.")
            try:
                pos = hub.pipes(now)
            except Exception:
                raise ModeError("I can't reach your pipe controller right now, so I haven't changed anything.")
            plan = self.plan(hid, mode)
            todo = [i for i in plan["close"] if pos.get(i) is None or pos[i].position != CLOSED]
            if cur:                                                   # switching from one mode to another: keep what is already off
                todo = [i for i in todo if i not in cur["state"].get("closed", [])]
            label = MODES[mode]["label"]
            if not todo and not cur:
                raise ModeError(f"Every pipe {label} would switch off is already off.")
            if not confirm and MUTANT != "no_confirm":
                return self._present(hid, mode, "needs_confirm", plan=plan, todo=todo, now=now)
            closed = list(cur["state"].get("closed", [])) if cur else []
            reopened = []
            if cur:                                                   # a pipe the old mode closed but this one keeps on comes back
                for i in [c for c in closed if c in plan["keep"]]:
                    a = svc._command(hid, None, i, OPEN, "mode", None)
                    if a.ok:
                        closed.remove(i)
                        reopened.append(i)
            failed, t_last = [], now
            for i in todo:
                a = svc._command(hid, None, i, CLOSED, "mode", None if MUTANT != "leased" else 120.0)
                if a.ok:
                    closed.append(i)
                    t_last = max(t_last, a.t_ack)
                else:
                    failed.append(dict(id=i, error=a.error))
            state = dict(closed=closed, failed=failed, keep=plan["keep"], protected=plan["protected"], hand=plan["hand"],
                         t_last=t_last, reopened=reopened, verified_lpm=None, verified_t=None)
            svc.store.set_house_mode(hid, mode, now, state)
            return self.status(hid)

    def status(self, hid: str) -> dict:
        svc = self.svc
        with svc._lock:
            now = svc.clock()
            cur = svc.store.house_mode(hid)
            if not cur:
                return self._present(hid, None, "off", now=now)
            st = cur["state"]
            if st.get("verified_lpm") is None:
                svc._poll(hid, now, min_gap=3.0)
                v = self._measure(hid, st["t_last"])
                if v is not None:
                    st["verified_lpm"], st["verified_t"] = round(v, 3), now
                    svc.store.set_house_mode(hid, cur["mode"], cur["since"], st)
            return self._present(hid, cur["mode"], "on" if st.get("verified_lpm") is not None else "verifying", state=st, now=now)

    def _measure(self, hid: str, t_last: float) -> Optional[float]:
        """Flow over the first full meter interval that starts after the last valve closed (litres a minute), or None yet."""
        rs = [r for r in self.svc.store.readings(hid, since=t_last) if r.t >= t_last + SETTLE_S]
        for a, b in zip(rs, rs[1:]):
            if a.boot_id == b.boot_id and b.t > a.t:
                return max(0.0, (b.out_total_l - a.out_total_l) / (b.t - a.t) * 60.0)
        return None

    def _home(self, hid: str, now: float) -> dict:
        svc = self.svc
        cur = svc.store.house_mode(hid)
        topo = svc._topology(hid)
        if not cur:
            p = self._present(hid, None, "off", now=now)
            p["speech"] = "No mode is on, so your pipes are as you left them."
            return p
        st = cur["state"]
        latched = {l["pipe"] for l in svc.store.latches(hid)}
        back, left = [], []
        for i in st.get("closed", []):
            if i in latched:                                           # kept closed after a leak since: the customer's other decision
                left.append(i)
                continue
            a = svc._command(hid, None, i, OPEN, "mode-end", None, force=True)
            (back if a.ok else left).append(i)
        svc.store.clear_house_mode(hid)
        label = MODES[cur["mode"]]["label"]
        p = self._present(hid, None, "off", now=now)
        said = join_or([topo.label(i) for i in back], "and")
        speech = (f"{_cap(label)} is off. {_cap(said)} {'is' if len(back) == 1 else 'are'} on again." if back else f"{_cap(label)} is off.")
        if left:
            speech += f" {_cap(join_or([topo.label(i) for i in left], 'and'))} stayed off" + (
                " because you asked me to keep it closed after a leak." if any(i in latched for i in left) else ": it didn't respond.")
        p["speech"], p["reopened"] = speech, back
        return p

    # ------------------------------------------------------------------ what Alexa says and the screen shows
    def _present(self, hid: str, mode: Optional[str], status: str, *, plan=None, todo=None, state=None, now=None) -> dict:
        svc = self.svc
        topo = svc._topology(hid)
        lab = lambda ids: join_or([topo.label(i) for i in ids], "and")
        pos = {}
        try:
            pos = svc._positions(hid, now if now is not None else svc.clock())
        except Exception:
            pass
        rows = svc._pipe_rows(hid, topo, pos)
        floor = svc.cfg.floor_lpm
        info = MODES.get(mode or "", {})
        label = info.get("label", "")
        cap = label[:1].upper() + label[1:]
        out = dict(kind="mode", mode=mode, status=status, label=cap, back_label=info.get("back"), pipes=rows,
                   simulated=bool(svc._source(hid).simulated), time_scale=svc.time_scale)
        if status == "needs_confirm":
            keep = [k for k in plan["keep"]]
            cur = svc.store.house_mode(hid)
            already = [i for i in (cur["state"].get("closed", []) if cur else []) if i not in keep]
            if todo:
                speech = f"{cap} {'also ' if already else ''}turns off {lab(todo)} until you say '{info['back']}'."
            else:
                speech = f"{cap} keeps off what is already off."
            if already:
                speech += f" {_cap(lab(already))} {'stays' if len(already) == 1 else 'stay'} off."
            if keep:
                top_keep = [k for k in keep if topo.nodes[k].parent not in keep] or keep
                speech += f" {_cap(lab(top_keep))} {'stays' if len(top_keep) == 1 else 'stay'} on."
            if plan["protected"]:
                speech += f" {_cap(lab(plan['protected']))} {'is' if len(plan['protected']) == 1 else 'are'} never switched."
            speech += (" Then I'll check the meter to make sure the water has really stopped. "
                       + ("Turn it off?" if len(todo) == 1 else "Turn them off?" if todo else "Shall I?"))
            out.update(speech=speech, to_close=todo, keep=keep, protected=plan["protected"], hand=plan["hand"])
            return out
        if status == "off":
            out.update(speech="No mode is on. Say 'sleep mode' or 'away mode' and I'll switch off the pipes nobody needs, then "
                              "check the meter.")
            return out
        st = state
        closed, keep, failed = st.get("closed", []), st.get("keep", []), st.get("failed", [])
        head = f"{cap} is on." + (f" {_cap(lab(closed))} {'is' if len(closed) == 1 else 'are'} off." if closed else "")
        if failed:
            head += f" {_cap(lab([f['id'] for f in failed]))} didn't respond, so {'it is' if len(failed) == 1 else 'they are'} still on."
        if st.get("hand"):
            head += f" {_cap(lab(st['hand']))} {'is a hand valve' if len(st['hand']) == 1 else 'are hand valves'}, so I left {'it' if len(st['hand']) == 1 else 'them'}."
        v = st.get("verified_lpm")
        if v is None:
            speech = head + " I'm checking the meter now."
        elif v < floor:
            speech = head + f" I checked the meter: no water is moving above what your sensor can read ({fmt_lpm(floor)})."
        else:
            kept_on = [k for k in keep if topo.nodes[k].parent not in keep] or keep
            if kept_on:
                speech = (head + f" But the meter still shows about {fmt_lpm(v)}. The {lab(kept_on)} stays on in {label}, so it "
                          f"could be there, or upstream of every pipe I can switch. If nobody is using water, say '{info['back']}' "
                          "later and ask me to find where it's going.")
            else:
                speech = (head + f" But the meter still shows about {fmt_lpm(v)} with every pipe I can switch turned off, so it's "
                          "upstream of all of them, or a valve didn't seal. If you're leaving, turn the water off at the main.")
        out.update(speech=speech, closed=closed, keep=keep, failed=failed, verified_lpm=v,
                   still_flowing=v is not None and v >= floor, since=svc.store.house_mode(hid)["since"])
        return out
