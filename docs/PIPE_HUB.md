# Pipe Hub — the optional controller that lets Alexa switch pipes and fill from taps

**Status: specified and emulated. No firmware has been written, no board has run it, no water has flowed through it.**
Everything in this repository that touches it was tested against a software model, `tools/pipehub_emulator.py`.
This file is the contract a real hub must satisfy and the checklist for building one.

## What it is, and what it is not

| | The water monitor's main safety valve | Pipe Hub (this document) |
|---|---|---|
| Job | Measures flow, **cuts the main supply on a leak** | Opens/closes **individual pipes** on request |
| Valve | One main safety valve; boots **closed**; re-armed only by hand at the device | One valve per pipe, resting **open**; optionally a valve on a tap it can fill from (an *outlet*), resting **closed** |
| Network commands | **None.** It has no command receiver | Yes, authenticated, leased |
| Can this add-on address it? | **No. There is no code path.** | Only if the operator configures one |

The Pipe Hub is a separate board with separate valves. The add-on never sends a command to the main safety valve, and
`set_pipe` refuses any name that is not a pipe in the household list.

**Smart plugs (Power Hunt) use the same contract.** A plug is a branch that rests ON, as a pipe rests open; a switch-off during a
hunt is a lease the plug controller ends by itself; a plug marked `protect` (a router, a freezer) is never commanded. In this
repository the plug controller is the same simulated hub (`VirtualPipeHub`) over the simulated home's plugs. Whether a given
commercial smart plug can end a lease on its own clock has not been checked, and a plug that cannot is not safe to hunt with.

Control exists only when the **operator** attaches a hub (`LEAKHUNT_HUB_URL` / `HuntService.configure_pipehub`). There is
deliberately no tool, prompt or voice command that can switch it on. A household without a hub gets the guided hunt only.

## Contract (server → hub, JSON over HTTPS)

All requests carry `Authorization: Bearer <token>`. HTTPS is required (plain HTTP only for `localhost`, or on a LAN if the
operator sets `allow_insecure_lan`).

```
GET  /api/v1/pipes            -> {"pipes":[{"id","position","lease_s_left"}, ...]}
GET  /api/v1/pipes/{id}       -> {"id","position","lease_s_left"}
PUT  /api/v1/pipes/{id}       {"want":"open"|"closed", "lease_s": number|null}
                              -> {"id","position","lease_s_left"}
```

* `position` is **measured**: `open | closed | moving | fault`. A hub that cannot measure must not report `closed`.
* Every valve has a **rest position**, its safe state: a pipe rests **open** (nobody is left without water), an outlet rests
  **closed** (nothing is left running).
* `PUT` is idempotent. A `want` away from rest with `lease_s` means *hold it there for at most `lease_s` seconds, then go back to
  rest*. For a pipe, `lease_s: null` means hold closed until told otherwise. **An outlet never opens without a lease**: `want:
  "open"` on an outlet with `lease_s: null` is answered `400`. `want` equal to the rest position clears any lease.
* **The hub enforces the lease with its own clock.** If the server, the network or the add-on dies, every valve goes back to
  rest: pipes reopen, outlets close.
* The hub caps `lease_s` itself and never trusts the caller's cap: **300 s** for a pipe, **60 s** for an outlet.
* `400` for a bad body, `401` for a bad token, `404` for an unknown pipe. Unknown fields are ignored.
* Typical answer is immediate with `position: "moving"`; the client then polls `GET /api/v1/pipes/{id}` until the position
  equals `want` (≤ 6 s) or `fault`.
* Boot state: every valve at rest (pipes **open**, outlets **closed**), no leases.

## Taps it fills from (outlets)

"Alexa, fill the bath, 120 litres" (or "fill the pot, 3 litres" at the kitchen tap, "water the plants" at the garden hose)
opens that tap's outlet valve on a lease and keeps that lease equal to the time the remaining litres need at the measured rate
(`addon/fill.py`). The add-on extends it only while its meter data is fresh, so the
hub's own timer closes the tap at about the amount even when the add-on, the network or the meter goes away. A real outlet needs:

* a valve that is **closed without power** (normally closed), so a power cut also ends at rest;
* a measured position, like every valve here;
* the 60 s lease cap enforced in the hub, independent of the add-on;
* an installer-set maximum per outlet (the tub's capacity less a margin), held in the household configuration (`max_l`).

If an outlet reports it did not close, or reports open again after it closed, the add-on closes the branch pipe above it, without
a lease, and says so. A fill does not start while the branch above the tap is closed, while a "keep it closed?" question holds a
pipe on a lease (it would reopen mid-fill), or while the hub cannot be reached. Nothing here
controls water temperature; the mix is whatever the tap is set to.

## Three states, kept apart

| State | What closes the pipe | What ends it | Who decided |
|---|---|---|---|
| **Test** | A hunt step, as a **120 s lease** renewed at half-life while sensor data is fresh | The next hunt step, Stop, the end of the hunt, or the lease running out on the hub's clock | The customer's spoken yes to the test |
| **Held for a question** | The end of a hunt that found a branch: that pipe is closed again on a **fresh 120 s lease** | An answer, or the lease running out | Still the yes to the test. Nothing is kept yet. |
| **Kept closed** | The customer's explicit "keep it off" (`set_pipe`, state `off`, on the held pipe): **no lease**, latched | Only the customer saying the pipe is repaired and turning it on (reopening asks "has it been repaired?" first) | A second, separate yes |

A lease is a recovery mechanism, not a claim that reopening is the safest outcome in every house. That is why the one state that
outlives the software is the customer's own decision.

## Precedence, from the top

1. **The main safety valve** is never addressed. If it is closed, the hunt aborts (`device_safety_valve_closed`), the add-on
   refuses to *open* pipes on request, and it sends no new close. Ending the hunt still puts back the pipes the hunt itself closed:
   with the main valve shut no water moves, and the hub's leases would reopen them anyway, so leaving them shut would only trap the
   customer later.
2. **Stop** is always allowed and idempotent. It ends the hunt and sends "open" to every pipe the hunt closed. It beats everything
   below. A pipe that cannot be confirmed open is **named** in the answer ("I couldn't confirm the shower is back on yet… within N
   seconds at the latest"), with the time its lease ends; `auto.not_restored` carries the same list.
3. **Lease expiry** on the hub's own clock. It needs no server and no network.
4. **A voice command** that moves a pipe during a hunt asks first ("Switching a pipe ends the hunt…") and then wins over the hunt.
5. **Hunt steps**, subject to the limits below.

## Stale data: what happens between 35 s and 60 s

| Age of the newest sensor reading | Behaviour |
|---|---|
| up to 35 s | Normal |
| over 35 s (`stale_s`) | **Paused.** The engine makes no inference. **No valve command of any kind is sent**, lease renewals included. The screen and speech say it is paused and give the time left. If fresh data returns the hunt resumes where it was. |
| over 60 s (`STALE_ABORT_S`) | **Abort**: the hunt ends and every pipe it closed is sent "open". |

A pipe whose lease lapses while paused is noticed on the next check and ends the hunt (`pipe_moved`); either way everything is
restored.

## What the add-on does with it (all in `addon/service.py`, covered by `tests/test_auto.py`, `tests/test_pipehub.py`)

* Auto hunts need a spoken **yes** to the consent text (`consent=True` only after a yes). The consent text says that a pipe will
  only be kept off if the customer says yes again.
* Every close during a hunt, and the hold at its end, is a **120 s lease**.
* A spoken "off" is the customer's own command: it is sent with no lease and stays closed until she says "on". If that pipe is the
  one a hunt is holding for a question, the same "off" is the explicit yes and also latches it.
* Telemetry older than 60 s, a hub unreachable for 15 s, or 60 commands in one hunt → **abort and reopen everything**.
* A valve that acknowledges more than 5 s after the command aborts the hunt (`pipe_too_slow`): the engine's timing assumes
  the close happened when it was reported.
* A pipe is never flipped twice within 2 s (restores and lease renewals bypass this, because getting a pipe open matters more than relay wear); a voice command that moves more than 2 pipes needs a yes.
* A mis-heard pipe name is repaired at most one word and **asked back** (“Did you mean kitchen pipe 1?”) before anything moves.
* `protect: true` pipes (water heater feed, sprinklers, anything safety-critical) are never switched.
* Every command is written to the `actuation` audit table (who asked: `voice`, `hunt-start`, `hunt`, `hunt-end`; ok; error; lease renewals are not logged one by one).
* If the main safety valve is closed, the add-on **refuses to open pipes** until the customer re-arms it by hand.

## Requirements for a real hub (not yet satisfied by any hardware)

1. **Fail-open by physics.** Prefer *normally-open* valves, energised to close, so losing power opens the pipe. If a motorised
   ball valve is used, it keeps its last position on power loss, so the lease watchdog alone must be trusted: reboot to open.
2. **Lease watchdog in firmware**, driven by a hardware timer, not by network state. Test by pulling the network cable mid-lease.
3. **Measured position** (limit switch, hall sensor, current signature). A bare solenoid has none: its “closed” would only be
   electrical. The engine still checks every close against the flow meter, so a valve that did not really close cannot cause a
   false accusation, but it can cause a missed one. *Documented unknown, see HONEST_UNKNOWNS.*
4. **Manual override** on every valve (hand lever or bypass) and a physical “all open” button.
5. **Low-voltage only** (12/24 V DC, fused, isolated supply, flyback diode, IP-rated enclosure). No mains near water built by an
   amateur. Valves rated for potable water and the line pressure. Check local plumbing rules; a licensed plumber may be required.
6. **Water hammer.** Fast solenoids can spike pressure. Use slow-closing valves or arrestors. The software spaces commands (≥ 2 s)
   but cannot see pressure.
7. **Credentials.** One bearer token per hub, rotated by the operator; HTTPS with a certificate the add-on can verify.
8. **Reachability.** The add-on calls the hub, so the hub must be reachable from where the add-on runs (same LAN, VPN or tunnel).
   A home hub behind NAT can instead dial *out* to a relay that exposes this same contract to the add-on; the relay is **not built**,
   and its poll interval would have to keep the ack under 5 s.

## Bring-up checklist (each line is a test someone must actually run)

1. `python3 tools/pipehub_emulator.py` and drive it from the add-on: `tests/test_pipehub.py` is the executable version. Then run
   the separate open-source checker, **pipehub-conformance** (`pipehub_check.py URL --token T --pipes ... --outlets ...`), against
   the board: 13 checks of this contract over HTTP, including leases that end with nothing sent.
2. Board on the bench with LEDs instead of valves: boot state open; lease expiry on a stopwatch; network pulled mid-lease.
3. One valve on a bucket line at low pressure: travel time measured, `moving → closed` observed, fault injected (jam it).
4. Position truthfulness: confirm `closed` really means no flow with a flow meter in line.
5. Two valves: rate limit, ordering, both leases independent.
6. Only then a real room, with the hand valves within reach and someone watching.

## Tested vs not tested

| Thing | Tested how | Not tested |
|---|---|---|
| Client ↔ contract (HTTP, auth, leases, caps, errors) | `RestPipeHub` ↔ `pipehub_emulator` over localhost | Any real network, TLS, NAT |
| Lease expiry on the hub's clock | Emulator clock | A real MCU timer under brownout |
| The contract as a whole | pipehub-conformance's 13 checks against `tools/pipehub_emulator.py` over HTTP: all pass (21 Oct) | Any real controller |
| Hunt logic with a dark/stuck/slow hub | Service ↔ emulator, fake clock | Real valve behaviour |
| Water behaviour | Simulated additive-flow house | Real pipes, pressure, water hammer |
