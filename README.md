# Leak Hunt

**An Alexa+ add-on that looks after the water and the power in a home. Every day it runs water to the litre (a bath, a pot at the
kitchen tap, the garden hose) while your hands are busy elsewhere, and "good night" turns off the pipes nobody needs overnight and
checks the meter. When water keeps moving with every tap off, or the home keeps drawing power all night, it turns the manual
"process of elimination" into an assistant-led, repeatable investigation: it asks before it acts, switches pipes or plugs under
supervision, says plainly what it cannot see, and keeps a record that can be replayed.**

**Start here:** [one page: what it is, what is real, what is simulated](docs/SUBMISSION.md) · [the four rules Alexa follows when
she touches water](docs/RULES.md) · [run it and try to fool it](#try-to-fool-it) · [how every claim is checked](docs/VERIFICATION.md)
· [what is not known](docs/HONEST_UNKNOWNS.md)

**"Alexa, fill the bath, 120 litres,"** from the kitchen with your hands full. The bath tap opens only on a lease that the pipe
controller ends by itself, set to the time the remaining litres need, so the tap closes at about the amount even if Alexa stops
talking to it. If water was already moving somewhere else, Alexa says so when the bath is full and offers to find where it is going.
The same works at any tap with a valve on the controller: **"fill the pot, 3 litres"** at the kitchen tap, **"water the plants"**
at the garden hose.

Finding where that water goes is the leak hunt. When water keeps moving with every tap off, the usual advice is to shut off parts of the house one at a time and re-test. Leak Hunt
does that with you. With a small pipe controller (a board driving one valve per pipe) Alexa+ closes your pipes one after
another, watches the live flow meter react after each one, and names the branch only when the flow dropped, came back, and dropped
again by the same amount. Without the controller she guides you valve by valve instead. When the evidence isn't there she says so,
with a reason, and never says "no leak" for a flow she cannot see.

You can also just say **"Alexa, turn off kitchen pipe 2"**. Pipes are named by room and number, and when a name fits two pipes
Alexa asks which one instead of guessing.

**"Alexa, good night"** (or "sleep mode", like the modes on a phone) turns off every pipe the controller can switch except the
bathroom, after a yes, and then reads the meter: "no water is moving", or "water is still moving, and the bathroom branch stays on
in sleep mode, so it could be there". **"Away mode"** turns off the bathroom too. **"I'm up"** or **"I'm home"** turns back on
exactly what the mode turned off, never a pipe kept closed after a leak. A mode is the customer's own lasting decision, so the
controller keeps those pipes off even if the add-on goes away.

**"Alexa, what's using power while we sleep?"** Power Hunt is the same engine, the same safety rules and the same screen on a
whole-home energy meter and smart plugs. "Last night your home never drew less than 108 watts. Until two nights ago that was about
62 watts..." With a yes, Alexa switches plugs off one at a time on leases the plug controller undoes by itself, and names a plug
only when the drop repeats: "Confirmed: 46 watts is drawn behind the TV plug." Plugs the household marks must-stay-on (the router,
a freezer) are never switched and are not part of the hunt. A whole-home monitor can already tell you how big the always-on draw
is, and a plug with its own power meter can tell you what it draws; this finds which of your ordinary, unmetered smart plugs it is
behind, with one meter and measured evidence ([docs/PRIOR_ART.md](docs/PRIOR_ART.md)).

This repository is an Alexa+ add-on (a self-hosted MCP server with an MCP App screen), the engine behind it, and the spec and
emulator for the pipe controller.

> **What is simulated.** The demo house, *the pipe controller and the taps' valves*, and the demo home's *energy meter and smart
> plugs* are simulated, and the product says so on screen ("Simulated house · time-lapse", "Simulated home"). The engine, protocol, OAuth, screen and checks are real code that runs. **No firmware has been written and
> no real valve or plug has been driven, and no real energy meter has been read.** Every verification number below comes from simulation with an *assumed* sensor model; none of it
> is a statement about a real house. See [docs/HONEST_UNKNOWNS.md](docs/HONEST_UNKNOWNS.md).
>
> **What it is not.** Not a better leak sensor: it investigates sustained flow **above the configured sensor threshold** (assumed
> about 1 L/min, not yet measured), and existing products state finer drip sensitivity. The isolation procedure itself is established
> ([docs/PRIOR_ART.md](docs/PRIOR_ART.md)); the contribution is the supervised, repeatable, recorded form of it.

## Who it is for, and what it needs today

It is for a household that wants everyday help with water (a bath filled to an amount while they cook) and, when water keeps moving
with everything switched off, help finding which branch it is behind. The daily job and the rare one use the same meter and the
same controller. Today that takes three things this repository does not ship and no Amazon device provides: a flow meter that reports cumulative litres, a way to
close one pipe at a time (**Guided Hunt**: shut-offs you can reach by hand, no extra hardware; **Auto Hunt**: a pipe controller with
one valve per pipe, specified in [docs/PIPE_HUB.md](docs/PIPE_HUB.md) and not built; filling also needs a valve on the tap it fills
from), and an Alexa+ host that runs the add-on. Power Hunt needs the same for electricity: a whole-home energy meter that reports a
running energy total, and smart plugs whose controller switches them back on by itself when a lease runs out.
Nothing here has been measured with a real sensor or meter, and no claim is made about how many homes have or would fit this
equipment.

## What it does

1. **Fill by volume, every day.** "Alexa, fill the bath, 120 litres", "fill the pot, 3 litres", "water the plants". Each tap's valve
   (the demo house has three: bath, kitchen tap, garden hose) rests closed and opens only on a lease
   that the pipe controller ends by itself; Alexa keeps that lease equal to the time the remaining litres need, so **the
   controller's timer is the fill timer** and the tap closes at about the amount even if Alexa stops talking to it. Water that
   was already moving is measured and left out of the count, measured again in one short pause during a long fill (so a tap
   turned off just before you asked cannot skew the count), and reported at the end with an offer to find where it goes. If
   the house's other water use changes mid-fill, it stops rather than over-fill; if the tap jams open, it closes the branch
   above it. It will not start a fill that cannot go as promised: with the branch above the tap switched off, while a "keep it
   closed?" question is open (that pipe would come back on mid-fill), without fresh meter data, or with the controller out of
   reach. It does not set the temperature. Filling by voice itself is not new (see [docs/PRIOR_ART.md](docs/PRIOR_ART.md)).
2. **Overnight finding, on request.** Ask "how's my water?" and it reads the cumulative flow counters, finds water that keeps moving
   in the quiet hours and compares it with the household's own earlier nights. (Alexa-initiated notification is not built.)
3. **Auto Hunt.** With a pipe controller attached, and after a spoken yes, Alexa closes and reopens the pipes herself. The
   customer only says when the house is quiet. Every close during the test is a **120-second lease on the controller's own clock**,
   so a dead server or dead network ends with the pipes open. "Stop" at any moment puts everything back and names any pipe it
   could not confirm.
4. **Keeping a pipe closed is a separate yes.** When the test points to a branch, that pipe is held on a fresh lease while Alexa
   asks "keep it closed until it's checked?". Only a yes makes it a latch that needs a confirmed "repaired" to reopen; "no" or
   silence lets it come back on.
5. **Guided hunt.** Needs no pipe controller or motorised valves (it does need live flow telemetry and shut-offs you can reach
   by hand): one instruction at a time, spoken and shown. The engine picks the most informative valve to try next.
6. **Replication.** A branch is *confirmed* only after drop, return, drop with matching size. The claim is: **the flow is isolated to
   the water behind that valve**.
7. **Completeness.** After confirming, only that valve is closed and everything else opened. If the flow reads zero the answer is
   "only these sources, above what the sensor can read"; otherwise "another source remains".
8. **Named pipes by voice.** "kitchen pipe 1", "the dishwasher", "bathroom". Ambiguity is asked back, a mis-heard word is checked
   ("Did you mean kitchen pipe 1?"), and "both" or "all" are understood.
9. **Honest answers.** "Nothing above what your sensor can read", "upstream of every valve you named", "this reading disagrees
   with the last time the valves were here, please re-check", and "I've paused: my sensor data is out of date".
10. **Evidence.** A packet with a SHA-256 hash, an HMAC code, the raw readings and every valve command, which a second program
   replays to the same result. It shows the record is unaltered and reproducible; it does not show the diagnosis is physically true.
11. **House modes.** "Sleep mode" / "good night" (the bathroom stays on), "away mode" / "I'm leaving for work", and "I'm up" /
   "I'm home". Asks first, switches without a lease (the customer's lasting decision), checks the meter over the first full
   interval after the last valve closed, and says where water could still be moving. Refuses during a hunt, a fill, or while a
   "keep it closed?" question is open; a hunt or a fill on a branch the mode switched off is refused with the mode named.
12. **Power Hunt.** `utility="power"` on the same tools: what the home drew at its lowest each night (and, if it stayed that high
   all year, the kilowatt-hours that comes to), then the same automatic or guided hunt over smart plugs, the same consent, stop,
   leases, "keep it off?" question and evidence packet, in power words (a test fails if any water wording reaches the customer).
   A plug that changed the draw by less than a stated bound is reported as "nothing larger than that is drawing through it", and
   the always-on power no plug carries is said to be on things it cannot switch.

## Try to fool it

The best way to judge a diagnosis tool is to attack it. Start the server with `LEAKHUNT_DEVHOST=1` (command below) and open
`http://127.0.0.1:8765/devhost?challenge=1`. **New hidden round** puts a leak (or no leak, or one below the sensor's limit) somewhere
in the simulated house and does not tell you where. Run the hunt on the screen, then throw things at it: a tap that someone switches
on or off, the sensor's link dropping, the pipe controller dropping, a valve jamming open, the sensor rebooting. **Reveal** fills a
verdict card beside the screen: what was hidden, what Leak Hunt said, whether that was right, and what the simplest rule ("the first
drop wins", replayed on the very same readings; a comparison, not a commercial product) would have said.

**Pull the plug.** The same column shows the simulated pipe controller: every pipe, and the time its own timer has left. Kill the
add-on while it has pipes closed and the screen goes silent ("No answer from the add-on"): no heartbeat, no lease renewals, no
commands, and the panel shows each pipe reopening when the controller's own timer runs out. Bring it back and it finds the pipes where the controller left
them and stops rather than guess.

* The claim is narrow and checked: with **at most one outside flow change** per hunt, Leak Hunt never names a valve that has no leak
  behind it. Two well-timed changes can fool it, and the page says so when you manage it ([docs/HONEST_UNKNOWNS.md](docs/HONEST_UNKNOWNS.md)).
* **Scripted: the tap stops mid-test** replays one unlucky moment found by searching 600 hunts
  (`tools/challenge_search.py`, [docs/check_challenge_search.json](docs/check_challenge_search.json)): a single outside change at each of 60
  timings, in 10 hidden rounds. Leak Hunt named a wrong valve in 0 of the 600; the "first drop wins" engine did in 7
  (on a timeline with sub-second phases: 0 and 6). It is a chosen illustration, not a typical hunt.
* Everything is the simulated house, with a fake or time-lapsed clock. The routes exist only with `LEAKHUNT_DEVHOST=1` and answer
  only to the machine they run on.
* The dev host is a real MCP client: its top bar lists every request it has sent to `/mcp` over Streamable HTTP (initialize,
  tools/list, resources/read, tools/call), and it takes the screen's address from the tool's metadata as a host would.

**See the same behaviour without a browser, in about a minute:** `python3 -I tools/repro.py` runs eight scenarios (a normal hunt,
the fooled naive engine, a house that will not hold still, stop, lost sensor data, a dead server with pipes closed, a tampered
evidence packet, a bath filled by volume and closed by the controller's own timer) on a fake clock, prints what Alexa would say, and checks that a second run is byte-identical
([docs/repro_output.txt](docs/repro_output.txt)).

## What it will never do

* It never addresses the **main safety valve** (the device's own whole-house cut-off). That valve is re-armed only at the device,
  and this add-on has no code path to it.
* It cannot switch pipes unless the **operator** attached a Pipe Hub (`LEAKHUNT_HUB_URL`). No tool, prompt or voice command can
  turn that on.
* It never runs an automatic hunt without a spoken yes, never closes a pipe during a hunt without a lease the controller enforces,
  never keeps a pipe closed beyond that lease without a second, explicit yes, sends no valve command while its sensor data is
  stale, and never switches a pipe marked `protect` (water heater, sprinklers). A pipe you turn off by voice is your own command
  and stays off until you turn it on.
* It never opens a tap without knowing when it will close: a tap's valve opens only on a lease of at most 60 seconds that the
  controller ends by itself, a fill has a per-tap maximum, it is never extended on stale meter data, and it never runs during a
  leak hunt. It controls how much water flows, never its temperature.
* A lease is a recovery mechanism, not proof that reopening is the safest outcome in every house; that is why a kept pipe is
  the customer's decision.
* Power Hunt never switches a plug the household marked must-stay-on (a router, a freezer), never asks you to, and switches every
  other plug off only on a lease the plug controller undoes by itself. House modes are for pipes only.

Details and requirements for a real hub: [docs/PIPE_HUB.md](docs/PIPE_HUB.md).

**Open-source companion: [pipehub-conformance](https://github.com/arulmuruganmcse2024-tech/pipehub-conformance).** The controller contract is also a separate MIT project that anyone building a
valve controller can run against their hardware over HTTP: 13 checks (leases end on the controller's own clock with nothing sent,
taps never open without a lease, caps, auth, malformed commands), a reference controller, and nine deliberately broken controllers
that the checker must catch (each fails exactly its own checks). It is its own public repository
(linked above); it has been run only against software, never against a real valve.

## Layout

| Path | What |
| --- | --- |
| `huntkit/` | The engine: pure function of (readings, reports). `core.py`, `engine.py`, `baseline.py`, `pipes.py` (names and voice resolution), `sim.py`, and the verification harness `check.py`, `mutants.py` |
| `addon/` | The Alexa+ add-on: `server.py` (MCP tools), `service.py` (hunts, auto runner, safety policy), `fill.py` (filling by volume), `modes.py` (sleep / away / home), `power.py` (Power Hunt: energy meter and smart plugs), `vocab.py` (the power words, and the check that no water wording is left), `actuators.py` (pipe hubs: branch pipes rest open, taps rest closed), `store.py` (SQLite), `sources.py` (the simulated house), `challenge.py` (dev host only: hidden leak, chaos, reveal), `oauth.py`, `ui/console.html` (MCP App), `ui/devhost.html` (host emulator and challenge page) |
| `tests/` | Unit tests, real-HTTP tests of the server and OAuth flow, and tests of the REST hub client against the emulator |
| `tools/` | `run_checks.py` and `verify_all.sh` (engine verification), `auto_fuzz.py` (automatic-hunt fuzzer), `fill_fuzz.py` (fill fuzzer), `power_fuzz.py` (random simulated homes for Power Hunt), `outcome_breakdown.py`, `repro.py` (eight scenarios, deterministic), `challenge_search.py`, `phase_grid.py`, `m4_analysis.py`, `pipehub_emulator.py`, `ui_check.py` (UI conformance audit), `devserver.sh` |
| `docs/` | `VERIFICATION.md`, `RULES.md`, `PIPE_HUB.md`, run logs, UI audit and screenshots, `FRICTION_LOG.md` (platform friction, official fields), `ENGINEERING_LOG.md` (my own bugs and what found them), product feedback, disclosure, unknowns, demo script |

## Run it

Python 3.12+ and the MCP Python SDK 2.2.0 (`pip install "mcp==2.2.0" uvicorn`; `playwright` only for the UI audit).

```sh
# unit + HTTP tests (304 at the time of writing)
python3 -W ignore -m unittest discover -s tests -t .

# simulated house with a simulated pipe controller, 6x time-lapse, host emulator at /devhost
LEAKHUNT_DEVHOST=1 LEAKHUNT_SPEED=6 LEAKHUNT_PUBLIC_URL=http://127.0.0.1:8765 LEAKHUNT_KEY=devkey python3 serve.py
# MCP endpoint: http://127.0.0.1:8765/mcp   health: /healthz   host emulator: /devhost

# In the first 20 simulated seconds after start the simulated meter's synthetic history meets its live readings, so a fill
# asked for then is refused with "your water meter has just restarted" (as it would be after a real restart). Ask again.

# the same house without a pipe controller (guided hunts only)
LEAKHUNT_CONTROL=0 python3 serve.py

# a software model of a pipe hub, for the REST client
python3 tools/pipehub_emulator.py --port 8099 --token demo
```

### Environment variables

| Variable | Meaning |
| --- | --- |
| `LEAKHUNT_MODE` | `virtual` (default, simulated house). The `device` mode (a real flow sensor) needs a sensor adapter that this build does not include |
| `LEAKHUNT_CONTROL` | `virtual` mode only: `0` removes the simulated pipe controller (default `1`) |
| `LEAKHUNT_HUB_URL`, `LEAKHUNT_HUB_TOKEN` | Real-sensor mode only: a Pipe Hub per docs/PIPE_HUB.md. **Setting the URL is the operator's decision and the only way to enable pipe control** |
| `LEAKHUNT_DB` | SQLite file (default `leakhunt.db`); hunts resume across sessions and restarts |
| `LEAKHUNT_KEY` | HMAC key for evidence links (random if unset, so links die on restart) |
| `LEAKHUNT_SPEED` | Time-lapse factor for the simulated house (default 1). Simulated time moves in exact one-second steps, one per tick, so the ticker runs this many times a second; a real device always runs at 1 |
| `LEAKHUNT_TICK_S` | Real seconds between the automatic runner's ticks (default 1 / `LEAKHUNT_SPEED`) |
| `LEAKHUNT_PUBLIC_URL` | Public base URL; required for OAuth and evidence links |
| `LEAKHUNT_DEVHOST` | `1` serves the host emulator at `/devhost` |
| `LEAKHUNT_OAUTH` | `1` turns on account linking |
| `LEAKHUNT_OAUTH_CLIENT_ID`, `LEAKHUNT_OAUTH_CLIENT_SECRET` | The pre-registered client (no dynamic registration) |
| `LEAKHUNT_OAUTH_REDIRECTS` | Comma-separated redirect URIs **from the Alexa+ developer console** |
| `LEAKHUNT_PAIRING` | `CODE:household,...` pairing codes given to households at setup |
| `PORT` | Listen port (default 8765) |

## MCP tools

| Tool | Purpose | Screen |
| --- | --- | --- |
| `water_status` | Overnight finding and current flow | yes |
| `start_leak_hunt` | With a controller, first asks "run it yourself or guide me?"; `mode="auto"` needs `consent=true` after a spoken yes; resumes within 30 minutes; optional `suspect` boosts a valve's prior for this hunt only | yes |
| `stop_leak_hunt` | Stops now and puts back every pipe it closed | yes |
| `list_pipes` | Pipes by room and number, on or off, which can be switched | yes |
| `set_pipe` | `"kitchen pipe 2"` on/off; asks which pipe when a name is ambiguous; `confirm=true` only after a yes to the tool's own question; `off` on a pipe a finished hunt is holding answers its "keep it closed?" | yes |
| `report_step` | `quiet`, `close`, `open`, `undo` for a valve (guided hunts, and hand valves in automatic ones) | yes |
| `hunt_status` | Current instruction, claims, outcome, what Alexa is doing | yes |
| `fill_water` | `"fill the bath, 120 litres"`, `"fill the pot, 3 litres"`, `"water the plants"`; without litres, that tap's saved amount; asks which tap when the name fits more than one; refuses during a hunt, above the tap's maximum, without fresh meter data, with the branch above it off, while a "keep it closed?" question is open, or with the controller out of reach | yes |
| `stop_filling` | Closes the tap now | yes |
| `fill_status` | Litres so far, time to go, the controller's timer, how it ended | yes |
| `night_flow` | The overnight analysis alone | no |
| `evidence_packet` | Evidence packet with integrity codes (including every valve command) and a link | no |
| `house_mode` | `"sleep"` (bathroom stays on), `"away"`, `"home"`; asks first and switches only after `confirm=true`; then checks the meter; with no mode, the current mode and its check | yes |
| `set_household` | Valves by room and name (several unnamed pipes in a room are numbered), timezone. It cannot touch the pipe-controller setting | no |

`water_status`, `night_flow`, `start_leak_hunt`, `stop_leak_hunt`, `list_pipes`, `set_pipe`, `report_step`, `hunt_status` and
`evidence_packet` also take `utility="power"` for Power Hunt (the energy meter and smart plugs). Without it they are about water,
except that a hunt is found by its id, `stop_leak_hunt` with no utility stops whatever hunt is running, and a `set_pipe` name with
"plug" in it is always a plug.

Each tool result carries `structuredContent`, readable `content` (the same words the screen shows), and `_meta.ui.resourceUri`
where there is a screen. The screen polls through `tools/call` because the default CSP is `connect-src 'none'`.

## How it is checked

Everything runs from the commands in [docs/VERIFICATION.md](docs/VERIFICATION.md), which also lists the current numbers, what each
check can and cannot show, and the real problems the checks found.

* bounded exhaustive exploration, **closed-loop fault injection** (a simulated customer follows the engine's current instruction while faults are injected at every position), random fuzzing, mutation testing, tightness searches
* an end-to-end **automatic-hunt fuzzer** with a fake clock, and a mutation run of that fuzzer (eight deliberately broken services, all caught)
* **Power Hunt**: tests and a random-homes fuzzer (`tools/power_fuzz.py`) checking that no water wording reaches the customer, no must-stay-on plug is switched or asked for, no plug that draws nothing is named, every plug switched off comes back on (also when the add-on dies or the customer says stop), and no "nothing larger than this" bound is below what the plug really draws; two deliberately broken variants, both caught
* **house modes**: asks before anything moves, no lease on a mode's pipes, the meter check, "I'm home" restores only what the mode switched off; two deliberately broken variants, both caught
* a **fill fuzzer** over all three taps, checked against each tap's true litres (water already moving, other taps switched just before or during the fill, lost meter data, a dead add-on, an unreachable controller, a jammed tap, the branch above switched off, stop), with seven deliberately broken fills, all caught
* a **search over 600 challenge hunts** with one outside flow change (twice: whole-second and sub-second timelines), against a deliberately naive engine
* a **deterministic reproduction** (`tools/repro.py`) whose output is committed and re-checkable with `--check`
* the REST hub client against a software hub model over real HTTP
* an **outcome breakdown** of the same 300 automatic-hunt scenarios, so restraint is not mistaken for usefulness: with a findable leak, one outside change at most and a customer who answered, it named a valve with a leak behind it in 71 of 72 and a dry valve in 0
* a UI audit with Playwright: touch-only completion, 48 px targets, WCAG contrast, light and dark, phone / inline / fullscreen, spoken/displayed parity, the verdict visible without scrolling, pull the plug, and the dev host's MCP requests; each new check is also run against a deliberately broken page and must fail there

## Built during the hackathon

Everything in this repository was written during the submission period, with AI coding assistance. See
[docs/DISCLOSURE.md](docs/DISCLOSURE.md).

## Licence

MIT. See [LICENSE](LICENSE).
