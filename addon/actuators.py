"""Pipe hubs: the only code in this repository that can move water.

A *pipe hub* is a small controller board driving one valve per pipe (relay + solenoid, or a motorised valve with a
position sensor). It is NOT the main safety valve. That valve stays under the device's own control: it boots closed and is
re-armed only locally. Nothing here can address it.

Two implementations of the same small contract:

  VirtualPipeHub  a simulated hub bound to the virtual house, including the lease watchdog a real hub must have
  RestPipeHub     a client for the Pipe Hub REST contract in docs/PIPE_HUB.md. It is tested only against
                  tools/pipehub_emulator.py; no real board has run it.

Contract in words
  * Every valve has a REST position, its safe state. A branch valve rests OPEN (nobody is left without water). An OUTLET
    valve (a bath or garden tap the add-on can fill from) rests CLOSED (nothing is left running).
  * A command is "hold this valve away from rest for at most `lease_s` seconds, then go back to rest". For a branch
    valve, `lease_s=None` means hold closed until told otherwise; an outlet is NEVER opened without a lease, and that
    lease is at most MAX_OUTLET_LEASE_S. The HUB enforces the lease with its own clock, so a dead server, a dead network
    or a crashed add-on all end with every valve back at rest: branches open, outlets closed.
  * The hub reports a MEASURED position (limit switch, hall sensor, current sense), never an assumed one.
  * Every call answers with an Ack: ok, the position reached, and when it was confirmed.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from .sources import VirtualSource

OPEN, CLOSED = "open", "closed"
MAX_LEASE_S = 300.0
MAX_OUTLET_LEASE_S = 60.0                 # an outlet is never held open longer than this without being asked again


@dataclass(frozen=True)
class PipeState:
    pipe: str
    position: str                         # open | closed | moving | fault | unknown
    lease_left_s: Optional[float] = None  # seconds until the hub reopens it by itself; None = held until changed


@dataclass(frozen=True)
class Ack:
    ok: bool
    pipe: str
    want: str
    position: str
    t_cmd: float
    t_ack: float                          # when the position was confirmed (service clock)
    error: str = ""


class VirtualPipeHub:
    """A simulated pipe hub: instant valves, a lease watchdog, fault injection for tests."""

    name = "virtual"
    simulated = True

    def __init__(self, source: VirtualSource, pipes: Iterable[str], outlets: Iterable[str] = ()):
        self.source = source
        self._outlets = [o for o in outlets]
        self._pipes = list(pipes) + [o for o in self._outlets if o not in pipes]
        self.lease_until: Dict[str, float] = {}
        self.stuck: Dict[str, str] = {}           # pipe -> position it is stuck in (fault injection)
        self.offline = False                      # fault injection: the hub cannot be reached
        self.lapsed: List[tuple] = []             # (pipe, lease_until): every pipe this hub reopened because its lease ran out

    # ------------------------------------------------------------------ contract
    def has(self, pipe: str) -> bool:
        return pipe in self._pipes

    def is_outlet(self, pipe: str) -> bool:
        return pipe in self._outlets

    def rest(self, pipe: str) -> str:
        return CLOSED if pipe in self._outlets else OPEN

    def _move(self, pipe: str, want: str, at: float) -> None:
        if pipe in self._outlets:
            self.source.set_outlet(pipe, want == OPEN, at)
        else:
            self.source.set_closed(pipe, want == CLOSED, at)

    def pipe_ids(self) -> List[str]:
        return list(self._pipes)

    def expire(self, now: float) -> None:
        """The hub's own watchdog: a lapsed lease returns the valve to rest at the moment it lapsed."""
        for pipe, until in sorted(self.lease_until.items(), key=lambda kv: kv[1]):
            if until <= now:
                if not (pipe in self._outlets and pipe in self.stuck):     # a jammed outlet stays open: the hub can't close it
                    self._move(pipe, self.rest(pipe), until)
                del self.lease_until[pipe]
                self.lapsed.append((pipe, until))

    def pipes(self, now: float) -> Dict[str, PipeState]:
        if self.offline:
            raise ConnectionError("pipe hub offline")
        self.expire(now)
        closed = self.source.physical_closed()
        out = {}
        for p in self._pipes:
            if p in self._outlets:
                pos = self.stuck.get(p) or (OPEN if self.source.outlet_open(p) else CLOSED)
            else:
                pos = self.stuck.get(p) or (CLOSED if p in closed else OPEN)
            left = None
            if p in self.lease_until:
                left = max(0.0, self.lease_until[p] - now)
            out[p] = PipeState(p, pos, left)
        return out

    def command(self, pipe: str, want: str, now: float, lease_s: Optional[float]) -> Ack:
        if self.offline:
            return Ack(False, pipe, want, "unknown", now, now, "hub_unreachable")
        if pipe not in self._pipes:
            return Ack(False, pipe, want, "unknown", now, now, "unknown_pipe")
        if want not in (OPEN, CLOSED):
            return Ack(False, pipe, want, "unknown", now, now, "bad_command")
        self.expire(now)
        outlet = pipe in self._outlets
        if outlet and want == OPEN and not lease_s:
            return Ack(False, pipe, want, "unknown", now, now, "lease_required")    # an outlet never opens open-ended
        if pipe in self.stuck:
            return Ack(False, pipe, want, self.stuck[pipe], now, now, "stuck")
        self._move(pipe, want, now)
        if want != self.rest(pipe) and lease_s:
            self.lease_until[pipe] = now + min(float(lease_s), MAX_OUTLET_LEASE_S if outlet else MAX_LEASE_S)
        else:
            self.lease_until.pop(pipe, None)
        return Ack(True, pipe, want, want, now, now)

    # ------------------------------------------------------------------ test hooks (not part of the contract)
    def reboot(self, now: float) -> None:
        """A hub reboot: every valve returns to its rest position (branches open, outlets closed), every lease is forgotten."""
        self.lease_until.clear()
        for p in self._pipes:
            self._move(p, self.rest(p), now)


class RestPipeHub:
    """Client for the Pipe Hub REST contract (docs/PIPE_HUB.md). Never raises from `command`."""

    name = "rest"
    simulated = False

    def __init__(self, base_url: str, token: str, clock: Callable[[], float], *, timeout: float = 3.0,
                 ack_timeout_s: float = 6.0, poll_s: float = 0.25, sleep: Callable[[float], None] = time.sleep,
                 allow_insecure_lan: bool = False):
        u = urllib.parse.urlparse(base_url)
        local = u.hostname in ("localhost", "127.0.0.1", "::1")
        if u.scheme != "https" and not (local or allow_insecure_lan):
            raise ValueError("the pipe hub must be reached over https (or localhost)")
        self.base, self.token, self.clock = base_url.rstrip("/"), token, clock
        self.timeout, self.ack_timeout_s, self.poll_s, self.sleep = timeout, ack_timeout_s, poll_s, sleep
        self._known: Optional[List[str]] = None

    # ------------------------------------------------------------------ HTTP
    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:      # nosec - operator-configured URL
            return json.load(resp)

    @staticmethod
    def _state(row: dict) -> PipeState:
        left = row.get("lease_s_left")
        return PipeState(str(row["id"]), str(row.get("position", "unknown")), None if left is None else float(left))

    # ------------------------------------------------------------------ contract
    def pipes(self, now: float) -> Dict[str, PipeState]:
        body = self._call("GET", "/api/v1/pipes")
        rows = [self._state(r) for r in body.get("pipes", [])]
        self._known = [r.pipe for r in rows]
        return {r.pipe: r for r in rows}

    def has(self, pipe: str) -> bool:
        if self._known is None:
            try:
                self.pipes(self.clock())
            except Exception:
                return False
        return pipe in (self._known or [])

    def pipe_ids(self) -> List[str]:
        self.has("")
        return list(self._known or [])

    def command(self, pipe: str, want: str, now: float, lease_s: Optional[float]) -> Ack:
        t0 = self.clock()
        if want not in (OPEN, CLOSED):
            return Ack(False, pipe, want, "unknown", t0, t0, "bad_command")
        try:
            lease = None if not lease_s else min(float(lease_s), MAX_LEASE_S)     # the hub applies its own, tighter cap to outlets
            r = self._call("PUT", "/api/v1/pipes/" + urllib.parse.quote(pipe, safe=""), {"want": want, "lease_s": lease})
            st = self._state(r)
            deadline = t0 + self.ack_timeout_s
            while st.position != want and st.position != "fault" and self.clock() < deadline:
                self.sleep(self.poll_s)
                st = self._state(self._call("GET", "/api/v1/pipes/" + urllib.parse.quote(pipe, safe="")))
            t1 = self.clock()
            if st.position == want:
                return Ack(True, pipe, want, st.position, t0, t1)
            return Ack(False, pipe, want, st.position, t0, t1, "fault" if st.position == "fault" else "no_ack")
        except urllib.error.HTTPError as e:
            return Ack(False, pipe, want, "unknown", t0, self.clock(), f"http_{e.code}")
        except Exception as e:                                       # network, timeout, bad JSON: never raise from here
            return Ack(False, pipe, want, "unknown", t0, self.clock(), type(e).__name__)
