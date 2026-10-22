# Four rules for an assistant that moves water (and power)

Whenever Alexa+ acts on plumbing in Leak Hunt, whether filling a bath, putting the house into sleep mode or hunting a leak, she
follows four rules. Power Hunt (smart plugs and a whole-home energy meter) follows the same four with the same code: plugs rest on
as branch pipes rest open, and a plug marked must-stay-on is never switched, as a protected pipe is never switched. They are written
so that a machine can check them, and each one is checked; the last column of the table says where. They are offered as a pattern
for any Alexa+ action that changes the physical world. Nobody else has adopted them, and they have been checked only against
the simulated house and controller in this repository.

1. **Act only on a precise request or a spoken yes.** A leak hunt moves pipes only after a yes to a test Alexa has described. A
   fill starts only from a request that names the tap and the amount (or that tap's saved amount), up to the tap's maximum, and
   only when it can go as promised: not with the branch above the tap switched off, not while a "keep it closed?" question is
   open, not without fresh meter data or a reachable controller. Keeping a pipe closed after a hunt is a second, separate yes.
   A house mode says which pipes it will switch off and waits for a yes.
2. **Every move away from the safe state is a lease that the controller ends on its own clock.** Branch pipes fall back open, so
   nobody is left without water. Taps fall back closed, so nothing is left running. Alexa renews a lease only while her meter
   data is fresh. A dead add-on, a dead network or lost data therefore all end at the safe state. For a fill the lease is the
   time the remaining litres need, so the controller's timer is the fill timer. The one deliberate exception is a decision the
   customer made to last: a pipe they turned off by voice, kept closed after a leak, or switched off by a house mode stays off,
   and "I'm home" turns back on only what the mode turned off.
3. **Claim only what was measured, and measured twice where it names a cause.** A branch is named only after the flow dropped,
   came back and dropped again by the same amount. A fill reports the litres the meter measured, with water that was already
   moving left out, and a long fill pauses once to measure that other water again and correct its count. Where the sensor cannot see, Alexa says "nothing above what your sensor can read", never "no leak".
4. **When the meter stops measuring what you think it measures, stop and say so.** A house that will not hold still ends a hunt
   with "I can't give you a trustworthy answer". A change in other water use ends a fill before it can over-fill. A tap that
   jams open, or reports open again after it closed, gets the branch above it closed. A meter that restarts mid-fill stops it.

| Rule | Where it is enforced | What checks it |
| --- | --- | --- |
| 1 | `addon/service.py` (`start_hunt`, `set_pipe`), `addon/fill.py` (`start`), `addon/modes.py` (`set`) | Fuzzer check A11; `tests/test_auto.py`; `tests/test_fill.py` (limits, names, hunt and fill exclude each other, `StartGuards`); `tests/test_modes.py` (M1, M2; broken `no_confirm`); `tests/test_power.py` (P2; broken `protected_in_hunt`) |
| 2 | `addon/actuators.py` (the controller's watchdog), `addon/service.py` (renewals), `addon/fill.py` (the lease is the remaining time), `addon/modes.py` (no lease for a mode) | Fuzzer checks A1, A2, A9, A10; fill checks F1, F5, F8; `tests/test_pull_the_plug.py`; reproduction scenes 6 and 8; deliberately broken services `no_lease`, `stale_acts` and fills `long_lease`, `renew_stale`; mode checks M3, M4 (broken `leased`); power check P4, including a dead add-on |
| 3 | `huntkit/engine.py` (replication), `addon/fill.py` (count with the earlier flow left out; the re-check pause), `addon/modes.py` (the meter check) | Exhaustive and closed-loop engine checks; fill checks F2 and F3, including changes in the seconds before the request; `tools/outcome_breakdown.py`; deliberately broken engines M1 to M5 and fills `no_baseline`, `no_recheck`; mode check M5; power checks P3 and P5 (`tools/power_fuzz.py`) |
| 4 | `huntkit/engine.py` (unstable windows), `addon/fill.py` (change detection, jammed tap, meter restart) | Fuzzer check A3 within its bound; the challenge search; fill checks F2 and F7; deliberately broken fills `ignore_change`, `no_backup` |

What the rules do not cover: whether a real controller, valve or meter behaves as the simulation does, how an Alexa+ host
presents any of this, water temperature, and whether a diagnosis is physically true. Those are listed in
[HONEST_UNKNOWNS.md](HONEST_UNKNOWNS.md).
