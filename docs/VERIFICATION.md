# Verification

**Everything on this page comes from simulation.** The house, the sensor (an *assumed* model: about 1 litre per minute floor,
360 pulses per litre, modest relative variability) and the pipe controller are software. None of it is a statement about a real
house, a real sensor or a real valve. See [HONEST_UNKNOWNS.md](HONEST_UNKNOWNS.md).

Every check below, including the exhaustive depth-6 runs and the UI audit, was re-run on the final code on 21 and 22 Oct 2026
(IST). Logs and result files are in
this folder; each command writes the file named next to it. The state counts of different checks are **not added together**: they explore different things.

## Reproduce

```sh
# unit + real-HTTP tests: 304 tests
python3 -W ignore -m unittest discover -s tests -t .

# one command, eight scenarios on a fake clock, output committed and re-checkable (about a minute)
python3 -I tools/repro.py                  # docs/repro_output.txt, docs/repro_output.json
python3 -I tools/repro.py --check          # re-runs and compares with the committed output

# engine checks (huntkit/check.py), on a three-valve house: house -> { bathroom branch -> toilet, kitchen }
python3 -I tools/run_checks.py exhaustive 6 T_leaks,K_leaks        # docs/check_exhaustive_d6_T_leaks.json
python3 -I tools/run_checks.py exhaustive 6 both_leak              # docs/check_exhaustive_d6_both_leak.json
python3 -I tools/run_checks.py adaptive 2 T_leaks,K_leaks,no_leak  # docs/check_adaptive_k2_T_leaks.json
python3 -I tools/run_checks.py adaptive 2 both_leak,ambient_tap_then_stops   # docs/check_adaptive_k2_both_leak.json
python3 -I tools/run_checks.py skeleton                            # docs/check_skeleton.json
python3 -I tools/run_checks.py fuzz 100000 25 0                    # docs/check_fuzz_0.json
python3 -I tools/run_checks.py mutants                             # docs/check_mutants.json
python3 -I tools/run_checks.py tight2                              # docs/check_tightness_adaptive.json
python3 -I tools/m4_analysis.py                                    # docs/check_m4_equivalence.json (about 2.5 minutes)
sh tools/verify_all.sh                                             # every check in this block except the exhaustive runs and the UI audit (about 45 minutes)

# automatic-hunt fuzzer (service + hub emulator + fake clock), and its eight deliberately broken services
python3 -I tools/auto_fuzz.py 300 0 docs/check_auto_fuzz.json
for m in no_lease no_restore hand_valves stale_ignored stale_acts offer_unleased auto_latch no_remeasure; do
  AUTO_FUZZ_MUTANT=$m python3 -I tools/auto_fuzz.py 100 0 docs/check_auto_fuzz_mutant_$m.json
done

# challenge mode: 600 hunts with at most one outside flow change, Leak Hunt against a "first drop wins" engine (about 13 minutes each)
python3 -I tools/challenge_search.py                               # docs/check_challenge_search.json (whole-second timeline)
CHALLENGE_JITTER=1 python3 -I tools/challenge_search.py            # docs/check_challenge_search_jitter.json (sub-second phases)
python3 -I tools/phase_grid.py                                     # docs/check_phase_grid.json: the demo's scripted moment, 410 phases (about 5 minutes)

# the automatic-hunt fuzzer again on a timeline with sub-second phases
AUTO_FUZZ_JITTER=1 python3 -I tools/auto_fuzz.py 300 0 docs/check_auto_fuzz_jitter.json

# filling by volume: 300 random fills over three taps checked against each tap's true litres, and seven deliberately broken fills
python3 -I tools/fill_fuzz.py 300 0 docs/check_fill_fuzz.json
for m in long_lease no_baseline ignore_change renew_stale no_backup no_stop_close no_recheck; do
  FILL_MUTANT=$m python3 -I tools/fill_fuzz.py 150 0 docs/check_fill_fuzz_mutant_$m.json
done

# Power Hunt: 500 random simulated homes (base load, one always-on plug or none, auto or guided, a dead add-on, stop)
python3 tools/power_fuzz.py 500 7 docs/check_power_fuzz.json     # docs/run_power_fuzz.log (about 7 minutes)

# what the 300 automatic-hunt scenarios actually ended with: right valve, nothing named, or a dry valve (about 4 minutes)
python3 -I tools/outcome_breakdown.py                              # docs/check_outcomes.json

# what the stalled-hunt fix changed: the same 300 scenarios with and without it (about 4 minutes, two processes)
python3 -I tools/remeasure_diff.py                                 # docs/check_remeasure_diff.json

# UI audit (needs playwright; start the dev server first)
sh tools/devserver.sh start 8765 6 /tmp/ui.db
python3 tools/ui_check.py http://127.0.0.1:8765                    # docs/ui_check.json, docs/shots/
```

## Results

### Engine

The engine is checked against seven properties at **every prefix of every explored schedule**:

| | Property |
| --- | --- |
| P1 | Soundness: a confirmed source or branch is real (with at most one unmodelled flow change); "only these sources" is never claimed while an unblocked source exists (any schedule) |
| P2 | Clean window: every resolved measurement window holds only open-valve, no-leak, same-boot readings |
| P3 | Counts, durations and flows are finite and non-negative |
| P4 | If the newest reading shows the safety valve closed, the hunt is aborted, never done |
| P5 | A "no flow" claim is qualified by a limit at or above the sensor floor |
| P6 | Actions name only known valves, and the engine never tells anyone to reopen the safety valve |
| P7 | Delivering the same readings shuffled and duplicated gives an identical view (sampled) |

| Check | What it explores | States | Of which reached "confirmed" | Violations |
| --- | --- | ---: | ---: | ---: |
| Exhaustive, depth 6 | Every sequence of waits, valve closes and opens, with up to 2 faults, for three truths (toilet leaks, kitchen leaks, both) | 2,248,477 per truth | 0 | 0 |
| Closed-loop fault injection | A simulated customer follows the engine's *current* instruction while up to 2 faults (lost reading, safety cut-off, re-arm, reboot, ambient flow toggle, undo-after-doing) are injected at every position; five truths | 468,828 | 292,112 | 0 |
| Fixed-script fault injection | The script a compliant customer would follow, with up to 2 faults at every position; five truths | 1,123,920 | 282,181 | 0 |
| Random schedules | 100,000 schedules of depth 25 on each of two topologies | 2,416,987 and 2,416,875 prefix evaluations | 2,041 and 1,267 | 0 |

**Read the "confirmed" column.** A depth-6 exhaustive search is too short to reach a confirmation (a confirmation needs a
drop, a return and a second drop, each with its own wait), so it checks everything *around* that: clean windows, aborts,
refusals, the safety valve. The checks that do reach confirmations are the closed-loop and fixed-script runs and the random
schedules. Faults are: a lost telemetry tick, the device closing its own safety valve, the cut-off clearing, a device reboot, an
unmodelled flow starting or stopping (someone's tap), and a real undo.

### Mutation testing of the engine

Five deliberately broken engines. A checker that cannot fail proves nothing, so each should be caught.

| Mutant | Break | Result |
| --- | --- | --- |
| M1 | A single drop confirms a source | Caught: false branch, 1,596 states |
| M2 | Windows may contain closed-valve or leak readings | Caught: P2, 218,569 states |
| M3 | Two drops on the same side count as replication | Caught: false branch, 5,050 states |
| M4 | Readings of different boots are differenced as one stream | **Survives** the property checker (1,865,306 states). Explained below; not caught, so not counted as caught |
| M5 | M4, and the counter-based checks removed too | Caught: P2, window spans a reboot, 2,031 states |

**M4 survives, and here is why.** A window that spans a reboot is stopped by two separate barriers: the split on `boot_id`,
and the counter, uptime and clock checks. M4 removes only the first. The second barrier still refuses every window across a
reboot except one where the previous boot was younger than `clock_tol_s` (3 s) at the window's first reading, a boot loop:
the clock check forces `|B2 - B1| <= tol`, and boot 2 cannot start before the window's first reading, so that reading is at
most `tol` into boot 1. In that corner, for a constant flow q the counters are `q*u`, so `n/T = q`: the rate comes out right.

`tools/m4_analysis.py` checks this against the real engine code instead of the algebra alone: 4,698,756 schedules (one
reboot, sampling that differs between the two boots, timestamp offsets of 0 and +-2 s, six flow patterns including a flow
change at the reboot) through the real engine and through M4. Result (`docs/check_m4_equivalence.json`):

* M4 resolved a window across a reboot in 217,381 of them. The oldest first reading of boot 1 was 2.5 s into its boot (the
  bound is 3 s). With constant flow the measured rate was exact to 4.4e-16 L/min.
* In 22,559 of those the real engine had not resolved yet (`reboot_truncated` or `window_too_short`) and M4 decided on the
  glued window, so M4 is **not** behaviourally identical: it decides earlier. When the flow changes exactly at the reboot, the
  glued window averages two rates (65,009 of the cases where both engines resolve give different rates).
* So what the boot split adds is not a different answer under constant flow, it is not deciding on a window that mixes a
  reboot with a flow change. That mixture is one unmodelled flow change, which is already inside the stated bound
  (the tightness search below shows that fooling the engine takes two or three).

This is an argument plus a bounded grid, not an exhaustive proof, and it assumes timestamps agree with uptime within
`clock_tol_s`. The boot split stays in the engine as the cheaper, earlier barrier. Two tests pin the bound
(`RebootGuardIsBackedByTheCounterAndClockChecks`): if someone loosens the clock tolerance, the bound fails.

### How far the soundness claim reaches

`tightness_adaptive` searches for the smallest number of unmodelled flow changes (someone's tap switching on or off at the wrong
moments) that makes the closed-loop engine state something false.

| Truth | Smallest fooling count |
| --- | --- |
| No source, a tap running at the start | **2** |
| Toilet leaks, no ambient flow | **3** (a false claim of a second source, or a false "only these sources") |
| Kitchen leaks, a tap running at the start | none found up to 3 |

So the soundness bound claimed is **at most one unmodelled flow change**, and two coincident ones timed to the customer's steps
can make the engine state something false. The product does not hide that: see HONEST_UNKNOWNS.md.

### Challenge mode: a search, and what it means

`tools/challenge_search.py` runs the case the project says it can never be fooled by: a tap is already running when the hunt
starts and is switched off **once**, `t` seconds in, for 60 values of `t` (20 s to 197 s), in 10 hidden rounds: 600 hunts through
the real service on a fake clock, each given up to 1,000 simulated seconds. The same readings are judged by Leak Hunt and by a
deliberately naive engine that believes the first drop it sees (mutant M1). It runs twice: on a **whole-second timeline** (the one
every other check here uses) and on one with **sub-second phases** (the hunt starts a random fraction of a second off the sensor's
sampling grid, ticks last 0.8 to 1.3 s, the customer answers after 3 to 7 s), which is what a wall clock produces.

| 600 hunts each | Whole seconds | Sub-second phases |
| --- | ---: | ---: |
| Leak Hunt named a valve with no leak behind it | **0** | **0** |
| Leak Hunt ended having named at least one valve, every one with a leak behind it | 600 | 600 |
| Leak Hunt declined to name any valve | 0 | 0 |
| "First drop wins" engine named a valve with no leak behind it | 7 | 6 |
| Hunts still running when the budget ran out | 0 | 0 |

A hunt that was still running would be graded on what it had said so far; none was. **What the ten hidden rounds contain:** each has
one or two leaks above the sensor's limit (seeds 3, 5, 7, 8, 9, 10, 11, 12, 13, 14; the garden tap, the shower, kitchen pipe 1 or 2
and the toilet, between 1.7 and 3.8 L/min). There is no round with no leak and none with a leak below the limit, so this search shows
that an outside tap never made Leak Hunt name a valve without a leak, and that it still found a real leak every time. It says
nothing about how Leak Hunt behaves on a leak-free house or a sub-floor leak under outside changes; `tests/test_challenge.py` covers
those cases on fixed seeds, and that is an example, not a search.

**The scripted moment in the demo** (a tap that was already running stops 12 s after the 4th pipe is closed, i.e. in the middle of
that pipe's measurement) is one of the cases that fools the naive engine. It is an illustration found by search, not a typical
hunt, and 600 hunts are a search over a modelled house, not a rate for real houses. `tools/phase_grid.py` plays it for every start
offset from 0 to 9 s and every customer delay from 0 to 40 s on a whole-second timeline (410 runs): the naive engine named a dry
pipe in 410 of them, Leak Hunt named the right valve in 410 and a wrong one in 0.

**A finding about timing.** On a timeline with sub-second phases the same script often does *not* fool the naive engine. Tracing it
showed why: the hunt takes a different path. On a whole-second timeline a sensor sample lands exactly on the instant a measurement's
settling time ends, and the engine counts it; a fraction of a second later it falls outside the window, the measurement waits one
more reading, and every step takes 30 s instead of 20 s, so the scripted moment lands somewhere else. I found it when the UI
audit's challenge scenario failed on a dev host that ran on a wall clock, while the same script passed in the unit tests. Two
changes followed: the dev host now advances simulated time in exact one-second steps (`SteppedClock`, `tests/test_stepped_clock.py`),
the timeline of the checks, and the service-level fuzzer and this search were repeated on sub-second phases (above, and
`AUTO_FUZZ_JITTER`, below). Leak Hunt's own claim did not depend on the timeline in any run; the demo's scripted illustration did.

`tests/test_challenge.py` (21 tests) pins the promises the mode makes on a few fixed seeds and moments: a hidden leak is found
and the reveal agrees; a leak below the sensor's limit is not pretended to be found; a house with no leak names nothing; a dropped
sensor link, a dropped pipe controller, a jammed valve and a sensor reboot each end the hunt without a false accusation; one outside
flow change never produces one (five moments on three seeds); the round state sent to the page does not contain the hidden truth;
and the routes do not exist unless the dev host is on. They are examples, not a proof; the searches above are the wider net.

### Automatic hunt (service, policy and the hub)

`tools/auto_fuzz.py` runs whole automatic hunts on the simulated house with a simulated pipe hub and a fake clock, ticking the
service once a second as the server's ticker does. Each scenario draws zero, one or two leaks (including below the sensor floor),
hand valves, a customer who is quick, slow or absent or who says stop, voice commands in the middle of a hunt, an ambient tap and
faults (a stuck valve, a hub that drops off, a hub reboot, a telemetry gap, answers to the "keep it closed?" question that are
yes, no, silent or late). When the hunt cannot start because a pipe name is ambiguous, the scenario's customer answers
"kitchen pipe 2", as a person would. Checked after every tick, and once more after the hunt and the lease time have passed:

| | Check |
| --- | --- |
| A1 | When a hunt ends (hub reachable), and in any case once an outage plus the lease time has passed, every pipe the hub can move is open, except a pipe the customer explicitly said to keep closed, or a pipe held for that question while it is still being asked |
| A2 | No pipe is closed without a live lease, unless it is a pipe the customer asked to keep closed or the customer's own spoken "off" |
| A3 | No false accusation when there was at most one ambient flow change and no fault; scenarios outside that bound are counted and not treated as failures |
| A4 | Only pipes the hub can move are commanded; protected pipes and hand valves never |
| A5 | Every hunt ends: nothing stays active past the idle timeout |
| A6 | "Stop" puts every movable pipe back immediately (hub reachable) and the hunt reads as stopped |
| A7 | A hunt never exceeds its command allowance (plus restore moves) |
| A8 | A pipe is never flipped twice inside the minimum interval, except forced restores and lease renewals |
| A9 | No hunt keeps running on telemetry older than the stale limit |
| A10 | While sensor data is stale, **no valve command of any kind** reaches the hub, lease renewals included (counted at the hub, not in the audit table) |
| A11 | A pipe is kept closed beyond its lease only after an explicit yes, never by the hunt on its own |
| A12 | The held-for-a-question lease outlives the hunt window; a "no" or silence leaves the pipe closed no longer than the lease |
| A13 | Every pipe the add-on could not restore is named (`auto.not_restored`), except pipes the customer closed by voice afterwards |
| A14 | A hunt that holds a pipe closed keeps making progress (a valve step, a report, a question) at least every 150 s, unless it is waiting for the customer or paused on stale data |

| Run | Scenarios | Violations |
| --- | ---: | ---: |
| Real service, seed 0, whole-second ticks | 300 (and 5 fixed probes) | **0** |
| Real service, seed 0, sub-second phases (`AUTO_FUZZ_JITTER=1`) | 300 (and 5 fixed probes) | **0** |

In the whole-second run 110 scenarios were inside the proven bound and 71 of them ended with a correct finding; Alexa asked "which one?" and
was answered in 81; 0 false accusations occurred out of bound either. The rest ended in an honest outcome: only these sources 137, nothing above the limit 55, below the sensor floor 21, abandoned 18, stopped 27, partial 18, and aborted on a hub that went
unreachable 3, stale telemetry 8, a pipe moved by something other than the hub 6, or a pipe that did not move 4; 3 ended with an upstream remainder. On sub-second phases the paths shift (only these sources 119, stopped 39, partial 21) and the checks hold: 0 violations, 0 out-of-bound false accusations, 110 scenarios inside the bound and 70 with a correct finding.

Eight deliberately broken services, 100 scenarios each, plus the probes. Every one must be caught:

| Mutant | Break | Caught by | Violations |
| --- | --- | --- | ---: |
| `no_lease` | Closes carry no lease | A2, A1, A12 | 18,783 |
| `no_restore` | Nothing is put back at the end | A1, A6 | 60 |
| `hand_valves` | Tries to move hand valves | A4 | 132 |
| `stale_ignored` | Never pauses or aborts on stale telemetry | A9 | 209 |
| `stale_acts` | Pauses its inference but keeps sending valve commands (including renewals) while stale | A10 | 6 |
| `offer_unleased` | The pipe held for the "keep it closed?" question has no lease | A2, A1 | 139 |
| `auto_latch` | Keeps the pipe closed at the end of a hunt without asking | A11 | 45 |
| `no_remeasure` | Waits on an unclean measurement instead of measuring again | A14 | 2 |

All eight are caught. This checks the service's policy against my model of a hub. It cannot show that a real hub behaves that way.
(`stale_acts` first escaped this fuzzer; see the friction log. It is now caught by the hub-level count. `no_remeasure` and check A14 were added after the challenge search turned up a stalled hunt; see the problems below.)

### Filling by volume

`tools/fill_fuzz.py` draws 300 fills over the demo house's three taps (bath, kitchen tap, garden hose, each with its own
simulated rate, maximum and branch above it): a target from 1 litre to that tap's maximum; water already moving (none, a leak above
or below the floor, two leaks); someone else's tap running before the fill, switched on or off in the 25 s before the request,
and/or switched on or off during the fill (timed to land inside it); and at most one fault (lost meter data, the add-on dying, the
pipe controller unreachable, the tap's valve jamming open, the branch above it switched off by voice, stop). It ticks the service
once a simulated second and checks every fill against the litres the simulated tap really delivered, which the add-on never sees
(checks F1 to F9, listed in the tool and in `tests/test_fill.py`).

| Run | Fills | Violations |
| --- | --- | --- |
| Real fill policy, seed 0 | 300 | **0** |
| Real fill policy, seeds 1000 to 1999 (`docs/check_fill_fuzz_1000.json`, not part of `verify_all.sh`) | 1,000 | **0** |

* 157 fills at the bath, 88 at the kitchen tap, 55 at the garden hose; 184 fills were long enough to pause once and re-check.
* With no outside change before or during the fill, faults included, no tap delivered more than the amount; in clean fills the
  litres Alexa reported were within 0.10 of the truth.
* 47 fills had a change in the 25 s before the request. Where the fill re-checked, the worst over-fill was 0.03 litres; the
  short fills that do not pause never went over in this run (their residual is stated in `docs/HONEST_UNKNOWNS.md`).
* 69 fills had an outside change while filling. The worst over-fill among them was **2.81 litres**, and the worst difference
  between what Alexa said and what went in was 3.38 litres, both inside the documented limit for a change too small to see.
  In the 1,000-fill run the worst over-fill was 0.02 litres without an outside change and 5.86 litres with one, also inside
  that limit: a change below the detection threshold can last the rest of a long fill.
* Endings: done 144, add-on dead with the tap closed by the controller's own timer 33, stopped by the customer 30, controller
  unreachable 25, stopped on an outside change 22, jammed tap with the branch above closed 17, branch switched off 16, meter
  data lost 11, meter data lost during the re-check pause (tap left closed) 2.

Seven deliberately broken fills, 150 fills each. Every one must be caught:

| Broken fill | What it does wrong | Caught by | Violations |
| --- | --- | --- | --- |
| `long_lease` | The tap's lease is always 60 s instead of the time the remaining litres need | F2 | 5 |
| `no_baseline` | Water already moving is counted as the tap's water | F3 | 58 |
| `ignore_change` | No change detection | F2, F3 | 3 |
| `renew_stale` | Keeps extending the tap while the meter data is stale | F5 | 5 |
| `no_backup` | A jammed tap gets no branch closed above it | F7 | 11 |
| `no_stop_close` | Stop does not close the tap | F6 | 5 |
| `no_recheck` | A long fill never pauses to re-measure the other water | F2, F3 | 7 |

### House modes

`tests/test_modes.py` (13 tests) runs sleep and away mode on the simulated house with a fake clock and checks each run with
`mode_failures`: M1 it asked before switching anything, M2 nothing moved before the yes, M3 no pipe the mode switched off has a
lease (so the controller cannot reopen it), M4 none came back on by itself, including after an hour with the add-on dead, M5 the
meter check came in. Further tests pin that sleep mode keeps the bathroom on, that away mode says when water moves upstream of
every pipe, that "I'm home" turns back on only what the mode turned off (not a pipe that was off before, not one kept closed after
a leak), switching between modes, the words people use, and that modes, hunts and fills refuse to run over each other. Two
deliberately broken modes must be caught: `no_confirm` (switches without asking, caught by M1) and `leased` (puts the mode's pipes
on a lease, caught by M3 and M4). The UI audit's `modes_light_768x480` scenario does the same from the screen.

### Power Hunt (energy meter and smart plugs)

Everything here is the simulated home: a virtual energy meter whose running total is kept in watt-minutes, six smart plugs (two
marked must-stay-on), and a 62 W base load the hunt cannot switch. `tests/test_power.py` (15 tests) and `tools/power_fuzz.py` check
every run with `power_failures`: P1 no water wording reached the customer (every presentation and error), P2 no must-stay-on plug
was switched, asked for, or put in the hunt, P3 no plug that draws nothing was named, P4 every plug switched off is back on after
the hunt and its "keep it off?" question (the customer's own "keep it off" excepted; in a guided hunt, the person does what the
last line tells them), P5 no "nothing larger than this" bound is below what that plug really draws.

`tools/power_fuzz.py 500 7` draws 500 homes: a base load of 5 to 250 W; in 80 % of them one of the four switchable plugs draws 6 to
200 W all the time; automatic (337) or guided (163) hunts; in some, the add-on dies part-way (43) or the customer says stop (94).
It ticks the service once a simulated second (`docs/check_power_fuzz.json`).

| Run | Homes | Violations of P1 to P5 |
| --- | --- | --- |
| Power Hunt, one random stream (seed 7) | 500 | **0** |

* Of the 288 finished hunts in a home with an always-on plug, it named that plug in 272, named nothing in 16, and named another
  plug in 0. The 16 are homes where the plug drew 4 to 15 % of the base load (at most 22.3 W): too small to separate from a big
  base, so the hunt ends with "nothing larger than N watts is drawing through it" for each plug, which P5 checks is true.
* Endings: found a plug and the rest is on things it cannot switch 272, none of the plugs carries the always-on power 94, stopped
  92, add-on dead (every plug back on by the controller's own timer) 42.

Two deliberately broken variants must be caught: `no_translate` (water words reach the customer, P1) and `protected_in_hunt` (the
router and freezer plugs are put in the hunt, P2). `tests/test_server_http.py` runs a whole power hunt over real HTTP with the
follow-up calls naming no utility, checks that water stays the default, and reads the evidence page for water words. The UI audit's
two power scenarios (768x480 light, 360x640 dark) run the hunt by taps only and fail if any water word is visible on screen or in an
aria-label, if the heading is not "Power Hunt", if spoken and shown text differ, or if a house-mode button is offered for plugs;
the check is also run against a page with one line of water text injected and must fail there.

### What the 300 scenarios actually ended with

"No wrong valve" could also be achieved by a hunt that never names anything, so the same 300 scenarios (seed 0, whole-second ticks)
are sorted here by what was really hidden and what the hunt said at the end (`tools/outcome_breakdown.py`, `docs/check_outcomes.json`;
`tests/test_outcome_breakdown.py` checks that every figure in the table below is the one in the file). "Findable" means a leak above
the assumed 1 L/min floor was hidden. The scenario mix is the fuzzer's, not any real home's, so these are counts, not rates.

* **Inside the proven bound** (110 scenarios: at most one outside flow change, no fault, no voice command, no stop): a leak
  above the floor was hidden in 83. The hunt named a valve with a leak behind it in **71**, a dry valve in **0**,
  and nothing in 12: 11 where the simulated customer never answered (the hunt waits for "the house is quiet" and closes
  itself after 30 idle minutes) and 1 where it said the flow still moves with every valve closed (seed 154, true of the outside tap; see
  problem 8 below). With a customer who answered, quickly or slowly: **71 of 72**. Where nothing above the floor was hidden
  (27), it said so in 25 and the customer never answered in 2; it named a valve in 0.
* **Outside the bound** (190: two outside changes, faults, voice commands in the middle, "stop"): 105 of 134 findable
  hunts named a valve with a leak behind it and 0 named a dry one; 25 were stopped by a fault, by the customer or by a house that
  would not hold still. Across all 300, a dry valve was named 0 times.
* From the start of a hunt to a named valve (inside the bound): median 335 and at most 585 **simulated** seconds, including the
  simulated customer's delays. Nothing here says how long a real hunt takes.

| Bound | Group | Bucket | Count |
| --- | --- | --- | --- |
| in the proven bound | findable | total | 83 |
| in the proven bound | findable | named_a_valve_with_a_leak | 71 |
| in the proven bound | findable | named_a_dry_valve | 0 |
| in the proven bound | findable | stopped_by_the_house_or_a_fault | 0 |
| in the proven bound | findable | customer_never_answered | 11 |
| in the proven bound | findable | said_where_the_water_is_not | 1 |
| in the proven bound | findable | other_nothing_named | 0 |
| in the proven bound | nothing_to_find | total | 27 |
| in the proven bound | nothing_to_find | said_nothing_above_the_limit | 25 |
| in the proven bound | nothing_to_find | named_a_valve | 0 |
| in the proven bound | nothing_to_find | customer_never_answered | 2 |
| in the proven bound | nothing_to_find | stopped_by_the_house_or_a_fault | 0 |
| in the proven bound | nothing_to_find | other | 0 |
| outside the bound | findable | total | 134 |
| outside the bound | findable | named_a_valve_with_a_leak | 105 |
| outside the bound | findable | named_a_dry_valve | 0 |
| outside the bound | findable | stopped_by_the_house_or_a_fault | 25 |
| outside the bound | findable | customer_never_answered | 2 |
| outside the bound | findable | said_where_the_water_is_not | 2 |
| outside the bound | findable | other_nothing_named | 0 |
| outside the bound | nothing_to_find | total | 56 |
| outside the bound | nothing_to_find | said_nothing_above_the_limit | 51 |
| outside the bound | nothing_to_find | named_a_valve | 0 |
| outside the bound | nothing_to_find | customer_never_answered | 3 |
| outside the bound | nothing_to_find | stopped_by_the_house_or_a_fault | 2 |
| outside the bound | nothing_to_find | other | 0 |

### Reproduction, tests and screen

* **`tools/repro.py`** plays eight scenarios on a fake clock (a normal hunt with the kept-closed question, one unlucky outside
  change, a house that will not hold still, stop, lost sensor data, a dead add-on with a pipe closed, a tampered evidence packet,
  a bath filled by volume and then a fill whose add-on dies near the end),
  prints what Alexa would say, states facts that are computed rather than typed, and runs everything twice to check the output is
  byte-identical. The output is committed in `docs/repro_output.txt` and `docs/repro_output.json`; `--check` compares.
* **304 tests pass**: the engine, the baseline, pipe names and voice resolution, the service and the automatic runner, the MCP
  server and the OAuth flow over real HTTP (including a server with a pipe hub), the REST hub client against the emulator (leases,
  a hub that goes dark mid-hunt, a slow valve, a stuck valve), challenge mode, filling by volume (`tests/test_fill.py`: the clean fill at all three taps, water already moving, the add-on dying early and
  late, outside changes before and during the fill, the re-check pause and what happens when the house, the meter or the
  controller changes during it, lost meter data, stop, a jammed tap, an unreachable controller, the branch switched off, the start
  refusals, limits, names, and seven deliberately broken fills; `tests/test_pipehub.py` runs a fill through the REST client and the emulated controller), pull the plug (`tests/test_pull_the_plug.py`: with the add-on dead its runner and polling never run, it
  issues no command, every pipe it had closed reopens at the exact moment its lease ends, no pipe is closed longer than the 120 s
  lease, and looking at the controller does not change what it does; two deliberately broken variants, a controller that ignores
  its lease and a kill that leaves the add-on running, must fail the same check), the outcome breakdown, the demo script's timing (slots back to back, ending by 2:57, at most 3.0 spoken words a
  second), and the bench analysis, house modes (`tests/test_modes.py`) and Power Hunt (`tests/test_power.py`, and three of its tests over real HTTP).
* **UI audit: 17 scenarios, 0 failures** (light and dark; 360x640, 600x260 inline, 768x480 and 1280x800; guided and
  automatic hunts; the pipes screen; sleep mode; Power Hunt at 768x480 and on a phone; filling the bath, and 5 litres at the kitchen tap on a phone; the challenge page; pull the
  plug). The fill scenarios fill by taps only, require the amount to grow on screen, the controller's timer and Stop to be visible
  while filling, the count to end within 1.5 litres, and "Find where it's going" to start a leak hunt; the timer check is also run
  with the timer hidden and must fail there. They also check that the dev host really sent initialize, tools/list (14 tools),
  resources/read and tools/call and shows them in its top bar, and that this check fails when the strip is hidden. Each hunt is finished by taps only; targets are at least
  48 px; text contrast meets WCAG; spoken text equals displayed text; no JavaScript errors. On the challenge page (1920 x 940 window)
  the verdict card must be fully visible beside the screen without scrolling, name the right valve, show the simplest rule being
  wrong, say the scripted timing was chosen and say the comparison is not a commercial product. In the pull-the-plug scenario the
  add-on is killed with two or more pipes closed; no call from the screen may reach it while it is dead, the screen must be veiled,
  every pipe must reopen on the panel within the lease by the controller's own timer, and after a restart the hunt must stop rather
  than guess. Both checks are run once against a deliberately broken page (the column pushed under the screen; the veil removed)
  and must fail there. Screenshots are in `docs/shots`.

## Problems the checks found

These are the real ones. Each has a fix and a regression test; the note says what found it where I know (fault injection, the challenge search, the UI audit, a design review).

1. **A false accusation after undo.** The engine rewrote history on "undo", so flow measured while the valve really was closed
   could be attributed to the wrong valve state. Fix: undo is not retroactive; the epochs in between are withdrawn, not rewritten.
   *(Found by fault injection.)*
2. **A valve reported closed before the baseline was not tracked.** Closed-loop fault injection found a path through it. Fix:
   such valves are part of the engine's state, and it asks for them to be opened as an explicit step (`open_first`).
3. **The same valve positions read differently from last time.** A new rule withdraws the newer reading and asks the customer to
   re-check. *(Fault injection found the gap; the rule closes it.)*
4. **A stale clock in the service made Alexa ask for hands when she was about to move the pipe herself.** Fix: the service re-reads
   the clock after driving the hub; an invariant test checks the speech against who acts.
5. **A pipe id could hijack a spoken name.** Ids are now only a fallback and never compete with what the customer calls a pipe.
6. **The culprit was kept closed without asking.** A design review found that a person could come home to a closed pipe they never
   agreed to. Fix: a held-for-a-question lease and a separate explicit yes (A11, A12 and two mutants cover it).
7. **Valve commands while sensor data was stale.** The first design kept renewing leases between 35 s and 60 s. Fix: a paused hunt
   sends nothing (A10, mutants `stale_ignored` and `stale_acts`).
8. **A hunt that waited on an unclean measurement while a pipe stayed closed.** In the challenge search a few hunts were still
   running when their 1,000-simulated-second budget ran out (36 of 600 on the whole-second timeline, 10 of 600 on sub-second phases),
   and I could not tell a slow hunt from a stuck one without tracing one. An outside tap had stopped in the middle
   of a step, the engine called that window unclean and said "I'll re-measure", but the window keeps every reading since the step
   began, so it can never become clean, and nothing in the automatic runner started a new measurement. The pipe it had closed stayed
   closed under renewed leases for 30 minutes until the idle timeout, which a visitor pressing "Outside tap OFF" in challenge mode
   could have caused. Fix: the runner starts a fresh step after an unclean window, inside the existing
   limit of six that ends with "the water use keeps changing, so I can't give you a trustworthy answer". New check A14 (a hunt holding
   a pipe closed keeps making progress), a deliberately broken service (`no_remeasure`) that A14 catches, and two regression tests.
   *(Found by the challenge search, not by the earlier fuzzer: it asked for fewer outside changes at awkward moments.)*
   **What the fix changed** (`tools/remeasure_diff.py`, `docs/check_remeasure_diff.json`: the same 300 fuzz scenarios with and
   without it): 6 outcomes differ. Four hunts that used to sit until the idle timeout (2,056 to 2,178 simulated seconds,
   three of them tripping A14) now end after 505 to 615 s. In one of those, an in-bound scenario (seed 154: a leak behind kitchen pipe 2
   and an outside tap that starts mid-hunt), the stalled hunt had named kitchen pipe 2 while it sat there; the fixed hunt names nothing
   and says 1.9 L/min still moves with every valve closed, which is true of the outside tap. So one in-bound finding was traded for an
   honest "upstream remainder" (in-bound findings 72 to 71). Of the other two changes, one hunt that ended "partial" now ends "only these
   sources" with the same valve, and one that ended "only these sources" now ends "partial" (a weaker statement) naming the same two
   valves, after 545 s instead of 1,055 s.
9. **The demo's scripted moment depended on the clock's phase.** The UI audit's challenge scenario failed on a dev host that ran
   on a wall-clock multiplier: with sub-second phases the hunt took a different path and the scripted tap-stop no longer fooled the
   naive engine. Fix: the dev host steps simulated time in exact seconds, like the checks; the fuzzer and the challenge search were
   repeated on sub-second phases; `tools/phase_grid.py` pins the scripted moment across start offsets and customer delays.
   *(Found by the UI audit, not by the unit tests.)*
10. **A panel that showed a stale fact.** After the scripted tap stopped, the challenge panel still said "outside tap running"
    while the Reveal said it had been switched off. The panel now re-reads the round every 2 s and the UI audit asserts it.
    *(Found by looking at the screenshot.)*
11. **Harness bugs.** The `hand_valves` mutant escaped because the set of commanded pipes was computed after the mutation; one check
   wrongly flagged pipes that were stuck or that the customer had closed by voice; a quarter of the automatic-fuzz scenarios never
   started a hunt, because a suspect named "kitchen" correctly drew "Which one?"; and `stale_acts` escaped because renewals were not
   in the audit table the check read. Each was fixed and the run repeated.

12. **A fill that trusted a fixed change rule over-filled.** The first fill compared each meter interval with a fixed 1 L/min or
    15 % rule. An exploration run with someone else's 1.5 L/min tap switched off mid-fill over-filled the bath by 11.7 litres.
    Fix: windows of 1, 3, 6 and 12 intervals against the engine's own noise model; the fuzzer bound and the deliberately broken
    fill `ignore_change`. *(Found by an exploration script before the fuzzer existed, then turned into its checks.)*
13. **A stop line that claimed too much.** "I've put back every pipe I closed" was said even when the controller had not confirmed
    one. It now says the controller was told to, then names the pipe and when it reopens itself (`tests/test_pipehub.py`).
14. **Two fills that could not go as promised.** Extending the fill from the bath to a kitchen tap and a garden hose, a review
    found that a fill could start while a "keep it closed?" question was open (that pipe's lease would reopen it in the middle of
    the fill) or while the branch above the tap was switched off by voice (no water reaches the tap). `fill_water` now refuses both,
    and also refuses when the controller cannot be reached (`tests/test_fill.py`, `StartGuards`). *(Found by a design review.)*
15. **A tap that jammed while the fill finished its count.** The first fill fuzz over all three taps found one violation of F7
    (seed 171): the kitchen tap was closed and the fill was waiting for its last meter reading when the valve jammed and reported
    open; nothing closed the branch above it. Fix: while it finishes the count the fill re-reads the tap, and a tap that was closed
    and reports open again is treated as jammed. The first version of the fix compared against a position read before the close
    in the same tick and made 186 of 300 fills end as "jammed"; six unit tests failed and it was corrected before any run was
    recorded. Regression test added; the fill fuzz and all six broken fills were re-run. *(Found by the fill fuzzer.)*
16. **The dev host never listed the tools.** The official FAQ describes the accepted simulated route as a page that sends
    initialize, tools/list and tools/call over Streamable HTTP. The dev host sent initialize, resources/read (with a hard-coded
    screen address) and tools/call, but not tools/list. It now lists the tools, takes the screen's address from the tool's
    metadata, and shows every request in its top bar; the UI audit checks all four and fails when the strip is hidden.
    *(Found by re-reading the FAQ.)*
17. **A change just before the request skewed a whole fill.** A kitchen-tap screenshot said "Filling at 8.2 L/min" for a tap
    simulated at 7.6. The baseline is the flow over the one meter interval before the tap opens; the flow elsewhere had changed
    inside it. Probing the bath: someone turning off 2.5 L/min 3 to 8 s before asking made it over-fill by 20 to 33 litres, and
    no window inside the fill can see such a change. The fuzzer had never drawn one: it set the house's other flow 25 s before
    the request. A first fix (refuse when the last two intervals disagree) was tried and dropped: probing showed it refused
    harmless cases and missed the harmful ones. Fix: a fill expected to run 90 s or more pauses once, re-measures the other flow
    over two agreeing intervals, redoes the count and compares the reopened tap against its own corrected flow; a change during
    the pause ends the fill. The fuzzer now draws changes in the 25 s before the request. Its first allowance for them asked the
    fill's own record whether it had re-checked, so the broken fill `no_recheck` escaped; whether a re-check was due is now worked
    out from the inputs, and `no_recheck` is caught. Short fills keep the residual, stated in HONEST_UNKNOWNS. *(Found by looking at a screenshot, then by probing.)*

18. **A guided hunt left the person's valves closed and said nothing.** The Power Hunt fuzzer's check P4 (every plug back on
    after the hunt) failed for guided hunts that ended or were stopped: the person had switched plugs off by hand, the hunt ended,
    and no line told them to switch them back on. The water hunt had the same gap. A guided hunt that ends or is stopped now ends
    with "You can open the X valve again now" (for plugs, "switch ... back on") for everything the engine believes is still closed
    apart from the finding; the fuzzer's simulated person does what that line says, and P4 passes. *(Found by the power fuzzer.)*
19. **After an automatic hunt the valve list still said "Closed by Alexa"** for pipes the controller had already reopened: the
    list showed the engine's last measured state, not the controller's. After a hunt it now shows where the controller says each
    pipe is. *(Found by looking at a Power Hunt screenshot.)*
20. **`hunt_status` with no arguments ignored a running power hunt over HTTP.** A pydantic `Field(None, ...)` used as a plain
    parameter default reached the function as a truthy object, so the water service was always picked. Fixed with
    `Annotated[..., Field(...)] = None`; `tests/test_server_http.py` now runs a whole power hunt whose follow-up calls name no
    utility. *(Found by the HTTP test.)*
21. **The UI audit's water-wording check could not fail.** Its pattern was escaped twice; the first power run passed, and the
    check's own broken variant (a line of water text injected into the page) was not caught. Fixed; it is now caught, and the
    scenario fails if it is not. *(Found by the broken variant.)*

## What this does not show

* A real sensor, a real valve, a real hub, a real energy meter, a real smart plug, or a real Alexa+ host. No firmware exists; the Pipe Hub contract is my own and is
  tested only against my emulator.
* That the assumed sensor model is right. A one-hour bench session with a measuring jug turns the assumptions into measurements
  ([BENCH.md](BENCH.md)); it has not been done.
* Anything below the sensor floor. By design the product says so instead of guessing.
* That the properties above are complete. They are the ones I thought of; mutation testing is how I looked for the ones I missed,
  and M4 shows where it came up empty.
* That hunts take the same time on a real sensor. On a whole-second timeline a measurement step took 20 s; on sub-second phases
  30 s. Both are on the simulated sensor, and a real one will add its own jitter.
* That the naive-engine comparison is a fair picture of other products. It is a deliberately simple engine, used to show what the
  replication rule is for.
