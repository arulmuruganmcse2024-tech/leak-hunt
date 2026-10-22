# Honest unknowns

Everything here is something I have **not** measured, built or confirmed. Verification numbers are from simulation with an
assumed sensor model. None of it is a statement about a real house.

## Physical, unmeasured
* The flow sensor's real floor, noise, calibration and run-to-run spread. The model assumes about 1 L/min, 360 pulses per litre and
  modest relative variability. A bench session with a measuring jug replaces these with numbers.
* Whether a real toilet or tap gives the steady flow the model expects, or a pulsing flow that the stability test rejects.

## The pipe controller (Pipe Hub) — specified and emulated, never built
* **No firmware, board, relay or valve exists.** Everything that "moves a pipe" here is a simulation (`VirtualPipeHub`) or a
  software model speaking the REST contract (`tools/pipehub_emulator.py`).
* Position truthfulness: the engine assumes a pipe reported *closed* really is closed. A bare solenoid has no position sensor.
  A valve that silently fails to close cannot cause a false accusation (the flow would not drop) but can cause a missed one.
* Travel time, water hammer and valve wear are not modelled. The software spaces commands by at least 2 s and aborts if a pipe
  takes more than 5 s to confirm, but it cannot see pressure.
* The lease watchdog is modelled with a clock. A real microcontroller timer under brownout, reset loops and Wi-Fi loss is untested.
* Reachability: the add-on calls the hub, so the hub must be reachable (same network, VPN, tunnel). A hub that dials out to a relay
  is a design sketch only; the relay is not built.
* Electrical and plumbing safety (low-voltage supply, ratings, local plumbing rules) is a requirement list, not a tested design.

## Auto Hunt
* The search is **cumulative**: pipes stay closed while the search narrows, then everything is restored at the final check. On the
  simulated house a toilet leak takes about 80 simulated seconds and 3-4 valve commands. On a real house the water to several pipes
  is off for that long, and the real time is unknown.
* A customer who says "yes" without hearing the consent text is still a consent. The text is spoken and shown, and the audit log
  records the time, but the add-on cannot know what was heard.
* Hand valves and pipes the controller does not own still need the customer's hands; the hunt falls back to guided steps for those.
* At the end of a hunt the pipe(s) that carried the finding stay closed for up to 120 s while the assistant asks "keep it closed?"
  (a hub-enforced lease, so it reopens by itself if nobody answers). Only a spoken yes turns it into a latch. The add-on cannot know
  whether a "yes" was meant for that question, and the latch ends only when someone says to reopen it. If the finding were wrong, the
  customer would have a pipe off that they agreed to, and the screen and speech say which.
* While the sensor data is stale (35 s) the hunt pauses and sends no valve command at all; at 60 s it aborts and puts pipes back.
  Those two numbers are chosen, not measured; the real sensor's reporting gaps are unknown.
* **A hunt that stops making progress is caught only in the ways the checks look for.** The service now starts a fresh measurement
  after an unclean window (a stall the challenge search found, see `docs/VERIFICATION.md`), and check A14 flags any hunt that holds a
  pipe closed for 150 s without a step, a report or a question. A14 saw three stalls in 300 scenarios before the fix and none after;
  a stall of some other kind, or in a house shape the fuzzer never draws, would be found only by luck. The idle timeout (30 minutes)
  and the hub's 120 s lease are the backstops, and both are modelled.
* **With an outside tap that starts mid-hunt, a hunt can end without naming a leak that is there.** In one in-bound fuzz scenario
  (seed 154) the hunt ends with "none of the valves you told me about stopped the flow (1.9 litres per minute still moving)" while
  a real leak sits behind a valve: the sentence is about the flow that remains, which is true of the tap, and the hunt names nothing.
  It is a miss, not a false accusation, and it is what the claim allows. The wording "none of the valves stopped the flow" can read
  as stronger than the evidence.
* Stop and the end of a hunt reopen pipes on a best-effort basis. Any pipe the hub did not confirm is named, with the latest time
  its own lease reopens it. That latest time is the hub's 120 s lease, which is modelled, not tested on hardware.

## Limits of the method
* A wrong valve that is really operated is invisible if it changes the flow the way the right one would.
* A periodic source (a refilling cistern) can fool a short test. The baseline stability check refuses the obvious cases.
* **Unmodelled outside flow changes** timed to the steps: the smallest number found that makes the engine state something false is in
  `docs/check_tightness_adaptive.json` (2 for a house with no leak and an outside source, 3 for a leaking toilet; none found up to 3
  for a leaking kitchen with an outside source). The "no false accusation" claim is therefore stated as *at most one* such change.
  These are searches over a modelled alphabet, not proofs.
* Drips below the sensor floor are out of reach. The product says so.
* One of five deliberately broken engines (M4, no reboot guard) survives every bounded check. It is explained in
  `docs/VERIFICATION.md`: the counter, uptime and clock checks refuse a window across a reboot unless the previous boot was
  younger than `clock_tol_s`, and then the constant-flow rate is exact. That is an argument and a grid over a modelled device,
  not a measurement; how a real flow sensor and its firmware behave across a reboot is unknown.

## Challenge mode ("Try to fool it")
* The comparison engine ("the first drop wins", the engine variant M1 replayed on the very same readings) is the simplest possible
  rule, kept as an ablation. It is not a commercial product and says nothing about how any real leak product behaves.
* The scripted round ("the tap stops mid-test") is **a chosen illustration, not a typical hunt**. The verdict card says so on screen.
  It was found by searching one outside change at each of 60 timings in 10 hidden rounds for a moment where that simple rule is fooled
  (`tools/challenge_search.py`). The 600 hunts are a search over a modelled house; the counts are not a rate for real houses.
* **Two well-timed outside changes can fool Leak Hunt.** The claim is at most one. The Reveal grades the answer against what was
  hidden and says so when someone manages it.
* Statements about where the water is *not* (`upstream_remainder`, "nothing above the limit", "below the sensor floor") can be
  false beyond the one-change bound. The Reveal grades those against the truth too, instead of filing them under "declined".
* **The scripted moment depends on timing.** On a whole-second timeline it fools the naive engine for every start offset (0 to 9 s)
  and customer delay (0 to 40 s) tried (`tools/phase_grid.py`), and the dev host runs on one. On a timeline with sub-second phases the
  same script often does not, because the hunt then takes a different path (30 s steps instead of 20 s). Leak Hunt's own claim, no
  false accusation with one outside change, held on both timelines in the searches; the searches are over a model.
* The hidden truth lives in the dev host's simulated house, never in the engine. The routes exist only with `LEAKHUNT_DEVHOST=1`
  and answer only to the machine they run on. Nothing here says how a real sensor behaves when somebody opens a tap.

## "Pull the plug" (dev host only)
* The controller in the panel is the software model `VirtualPipeHub`; the kill is a switch in the dev host that stops the add-on's
  heartbeat while the simulated controller's watchdog keeps running. It shows the policy (a dead add-on ends with the pipes open,
  within the lease) and that the check notices when that fails (`tests/test_pull_the_plug.py`, `tools/ui_check.py`). It shows nothing
  about a real controller: a real one needs a real timer, specified in `docs/PIPE_HUB.md` and not built.
* The model applies a lapsed lease the next time anything looks at it and records the moment the lease ended, so the outcome does not
  depend on who looks or how often (tested). A real controller has no such luxury.
* The veil over the screen stands in for a host whose add-on does not answer. How the real host presents an unreachable add-on is
  not known.
* When the add-on comes back it finds the pipes where the controller left them and stops rather than guess ("isn't where I left it").
  What a real add-on process restart looks like to the hunt record is only modelled by this switch.
## Filling by volume
* The taps' valves, their flows (in the simulation: bath 11.4 L/min against 12 nominal, kitchen tap 7.6 against 8, garden hose
  14.2 against 15) and the meter are models. No outlet valve has been built. Which taps a real home would put a valve on, and
  each one's maximum, are an installer's choice; the demo's three are examples. A real one must be closed without power, report its measured position, and enforce the 60 s lease cap
  itself (`docs/PIPE_HUB.md`).
* The count is the house meter's, with the flow measured just before the tap opened left out. A change in the house's other
  water use is detected by comparing the mean flow of the last 1, 3, 6 and 12 meter intervals with the first two, against the
  engine's own noise model (`stable_z` and `rel_sd`, both assumptions to be measured on the bench). Over a two-minute window a
  persistent change smaller than about 0.7 to 0.9 L/min at the bath, 0.5 to 0.7 at the kitchen tap and 0.8 to 1.1 at the garden
  hose (with up to 4 L/min flowing elsewhere; shorter fills have shorter windows and higher thresholds) can go unnoticed; the tap
  then delivers up to that change times the minutes left, more or less than asked. In 300 random fills over the three taps
  (`docs/check_fill_fuzz.json`) the worst was 2.81 L over the amount with an outside change before or during the fill,
  and none went over without one. Larger changes stop the fill.
* A change in other water use in the seconds before the tap opens (someone turns a tap off just before asking) is inside the
  baseline, and nothing measured during the fill can see it. A fill expected to run 90 s or more therefore pauses the tap once,
  about a third of the way in, for roughly 25 s (two agreeing meter intervals), measures the other water again and corrects the
  count; a change during the pause ends the fill with the tap closed. Shorter fills (under 18 L at the bath, 12 L at the
  kitchen tap, 22.5 L at the hose, with the demo house's nominal rates) do not pause, so such a change can make them off by the
  change times the fill's length, about a minute and a half at most. Before the pause existed, someone turning off 2.5 L/min a few seconds before
  asking made the simulated bath over-fill by 20 to 33 litres.
* With the simulated meter's 10 s reading interval the fill closes within 0.5 L of the amount, so it tends to end slightly under.
  A slower or noisier real meter changes this.
* It controls how much water flows, never its temperature: the mix is whatever the tap is set to. A full bath is a drowning risk
  for small children and a hot mix can scald. The add-on says when the bath is full and does not drain it. A pot under the
  kitchen tap or a hose left on a path is only as safe as where it was left; the fill knows litres, not where they land. These are reasons a
  real product needs its own safety review, not problems this repository has solved.
* Anyone who can speak to the device can ask for a fill, within the tap's maximum. Household permissions are the host's business
  and are not modelled.
* "Water already moving" is the flow measured before the tap opened. It may be legitimate use (a dishwasher) rather than a leak,
  which is why Alexa says "if nothing should be running".

## House modes
* A mode's pipes are closed **without a lease** on purpose (it is the customer's lasting decision, like a spoken "off"), so a
  mode that should not have been switched on stays on until someone says "I'm home". The yes before it switches is the guard.
* Sleep mode keeps the room called "bathroom" on. A house whose topology names rooms differently would need its own rule; the
  demo house is the only topology it has run on.
* The meter check reads one full meter interval after the last valve closed. A tap someone opens a moment later is not covered,
  and a slow drip below the sensor floor reads as "no water is moving", which the speech qualifies as "above what your sensor
  can read".
* No scheduling: "good night" is spoken, not a timer. Alexa+ routines could call it; that has not been tried.

## Power Hunt (energy meter and smart plugs), simulated only
* **No real energy meter has been read and no real smart plug has been switched.** The demo home's 62 W base load and the 46 W
  behind the TV plug are invented for the demo, not measurements and not typical figures.
* The meter model assumes 1 watt-second resolution, 2 % window-to-window variability and a 5 W floor. A fridge or freezer
  compressor that switches on during a measurement breaks the steadiness the engine needs: it then calls the window unstable and
  measures again, and a home where that happens all the time will get "I can't give you a trustworthy answer". That has not been
  seen on a real meter.
* The safety argument needs the plug controller to switch a plug back on **by itself** when a lease runs out. Whether common smart
  plugs can do that on their own clock varies by product and has not been checked; a plug that cannot is not safe to hunt with.
* Switching a plug off cuts whatever is on it: a computer loses unsaved work, a set-top box may reboot. The consent text says what
  is plugged in loses power for a few minutes; the household has to mark such plugs must-stay-on. Nothing checks that they did.
* "Nothing larger than that is drawing through it" means exactly that: with a large base load the bound is large (12 W in the
  demo, more on a busier home), so a small standby draw behind a plug can be missed. The engine never calls such a plug the
  source, and it never says the plug draws nothing.
* A plug with its own power meter, a smart breaker with per-circuit metering, or a monitor that recognises devices may already
  answer the question without switching anything. Power Hunt is for ordinary unmetered plugs on one whole-home meter.
* How Alexa+ would choose between water and power from a spoken question is up to the host's model; the tool descriptions and
  rule 12 of the server instructions say when to pass `utility="power"`. It has only been exercised by tests and the dev host.

## Platform, unverified
* No Alexa+ console, device or simulator was used. The screen is tested against an emulator that follows the MCP Apps protocol as documented.
* Whether the host needs `capabilities.extensions`, which redirect URIs it uses, and whether it sends the `resource` parameter.
* Whether the host runs tools one at a time, forwards results of *spoken* tool calls into an open screen, and lets a screen keep
  polling for a couple of minutes. Whether a spoken "stop" reaches `stop_leak_hunt` while another call is in flight.
* How a host treats a tool that changes the physical world. The tools say `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`.

## Process
* All verification numbers are from simulation with an assumed sensor model.
* Prior art was checked on 20 Oct 2026 by opening five primary pages (`docs/PRIOR_ART.md`); two more patent numbers named by a reviewer could not be opened and are listed as unread. It is a positioning check, not a patent search, and says nothing about validity or infringement.
* No user test of the screen or the voice lines has been done.
