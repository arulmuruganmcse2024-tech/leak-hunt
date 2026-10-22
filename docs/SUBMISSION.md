# Submission text (one page; everything else lives in the repository)

Paste each block into whichever field of the submission form it fits. The form's own field names are not copied here because I
have not seen the form; read it against this page before submitting.

## One sentence

Leak Hunt turns the usual leak-finding advice, "shut off part of the house and re-test", into a conversation with Alexa+: with
one meter and one small pipe controller she closes pipes one at a time on leases that end by themselves, names a pipe only when the flow drops,
comes back and drops again by the same amount, and says what she could not see instead of guessing; the same controller fills a
bath to the litre and turns the house's pipes off with "good night", and the same engine, on an energy meter and smart plugs,
finds what a home draws all night.

## Description

**Every day.** "Alexa, fill the bath, 120 litres," from the kitchen. The bath tap's valve rests closed and opens only on a lease
that the pipe controller ends by itself. Alexa keeps that lease equal to the time the remaining litres need, so the controller's
timer is the fill timer: if Alexa stops talking to it, the tap still closes at about the amount. She counts only the bath's water,
leaving out flow that was already moving and measuring it again in one short pause a third of the way in, and stops rather than
over-fill if the house's other water use changes mid-fill. When
the bath is full she reports any water that kept moving elsewhere: "Before I opened the tap, about 2.0 litres a minute was already
moving somewhere else. If nothing should be running, I can find where it's going." That is how the rare job begins. The same
works at the kitchen tap ("fill the pot, 3 litres") and the garden hose ("water the plants"). She refuses a fill that cannot go
as promised: with the branch above the tap switched off, while a "keep it closed?" question is open, without fresh meter data, or
with the controller out of reach.

**When water keeps moving.** When water keeps moving with every tap off, the usual advice is a process of elimination: close parts of the house one at a time
and re-test. Leak Hunt does that with you. The customer says "Alexa, find my leak". Alexa asks whether to run the test herself
or guide the customer, and an automatic hunt starts only after a spoken yes. With a small pipe controller she closes and reopens
one numbered pipe at a time and watches the live flow meter; without one she guides the customer valve by valve, on the same
engine and the same screen (Guided Hunt needs no controller or motorised valves, but it does need live flow telemetry and
shut-offs the customer can reach).

She says "the flow is isolated to the water behind that valve" only after the flow dropped, came back and dropped again by the
same amount. After that she asks a separate question, "Do you want it kept off until it has been checked?"; only a yes keeps the
valve closed beyond its two-minute lease. When the evidence is not there she says so with a reason ("nothing above what your
sensor can read", "the water use keeps changing, so I can't give you a trustworthy answer") and never says "no leak" for a flow
she cannot see. Pipes can also be switched by voice ("turn off kitchen pipe 2"); a name that fits two pipes is asked back.

**Good night.** "Alexa, good night" (or "sleep mode", like the modes on a phone) turns off every pipe the controller can switch
except the bathroom, after a yes, then reads the meter and says whether water is still moving and where it could be. "Away mode"
turns off the bathroom too; "I'm up" or "I'm home" turns back on exactly what the mode turned off, never a pipe kept closed after a
leak. The controller keeps a mode's pipes off even if the add-on goes away, because a mode is the customer's own decision.

**The same engine on electricity.** "Alexa, what's using power while we sleep?" Power Hunt reads a whole-home energy meter: "Last
night your home never drew less than 108 watts. Until two nights ago that was about 62 watts, so about 46 watts more has been on
all night since then." With a yes, Alexa switches smart plugs off one at a time, each on a lease the plug controller undoes by
itself, and names a plug only when the drop repeats with matching size: "Confirmed: 46 watts is drawn behind the TV plug." Plugs
the household marks must-stay-on (a router, a freezer) are never switched and are not part of the hunt, and the power no plug
carries is said to be on things she cannot switch. Nothing in the engine changed: its running total is kept in watt-minutes, so
its rate is watts. A whole-home monitor such as Sense reports how big the always-on draw is ("the size of the problem", in its own
words) and works per device only with compatible smart plugs; Amazon's announcement of the Alexa Energy Dashboard says plugs without
energy monitoring are estimated from a wattage the customer enters. Power Hunt measures which ordinary, unmetered plug it is behind,
with one meter. The demo home, its meter and plugs are simulated.

**Try to fool it.** The dev host has a challenge mode: a leak is hidden in the simulated house, the visitor runs the hunt and
interferes (an outside tap, the sensor's link dropping, a jammed valve), and *Reveal* shows what was hidden, what Leak Hunt said
and what the simplest rule, "the first drop wins", would have said on the same readings (a comparison, not a commercial product).
In a search over 600 hunts with one outside flow change, Leak Hunt named a wrong valve 0 times and the simple rule 7 times (and 0
and 6 on a timeline with sub-second phases). The
scripted round on screen is a chosen timing, and the verdict card says so. Two well-timed outside changes can fool it, and the
page says so. *Pull the plug* kills the add-on while it has pipes closed: the screen goes silent and the simulated controller's
panel shows every pipe reopening on its own timer.

**Not just restraint.** In the 300 automatic-hunt fuzz scenarios, with a leak above the sensor floor, at most one outside flow
change and a customer who answered, it named a valve with a leak behind it in 71 of 72 and a dry valve in 0 (`docs/check_outcomes.json`;
simulated house and customer, so these are counts, not rates).

**Why it matters for Alexa+.** A leak hunt is needed rarely; filling a bath, saying good night and checking the house happen every
day. One meter and one controller per supply give a household a daily reason to talk to Alexa+, and the same hardware does the rare,
high-stakes job. Power Hunt shows the engine is not about water: anything with a meter on the supply and switches on the branches
can be searched the same way, under the same rules. The four rules Alexa follows when she moves the physical world
(`docs/RULES.md`) held unchanged for plugs. No figures for adoption or savings are claimed.

**Safety.** Only the operator can attach a pipe controller; no tool or voice command can. A tap opens only on a lease of at most 60
seconds, a fill has a per-tap maximum and never runs during a hunt, and a tap that jams open gets the branch above it closed. Every close during a hunt is a
120-second lease enforced on the controller's own clock, so a dead server or network ends with the pipes open. "Stop" puts every
pipe back and names any it could not confirm. Stale sensor data pauses the hunt at 35 s (no valve command of any kind) and aborts
it at 60 s. The device's own main safety valve is never addressed.

## What is real and what is simulated

Real code that runs and is tested: the hunt engine, an MCP server (Streamable HTTP, MCP Python SDK 2.2.0) with an MCP App screen
and OAuth 2.1 + PKCE account linking, the pipe policy and the REST client for a pipe controller, and the checks.
**Simulated:** the house, the flow sensor model, the pipe controller and the taps' valves; for Power Hunt, the energy meter, the
smart plugs and their controller. No firmware exists, no real valve or plug has been driven and no real energy meter has been read.
Every verification figure comes from simulation with an assumed sensor model and is not a statement about a real house. The demo is
a clearly labelled simulated Alexa+ experience: the official FAQ says no participant can connect a self-hosted MCP server to a real
Alexa+ device or developer console, and describes the accepted route as a web page that acts as an MCP client. The dev host is
that page: it sends initialize, tools/list, resources/read and tools/call to the server over Streamable HTTP, lists each request in
its top bar, and renders the MCP App screen in a sandboxed iframe. It is labelled "Not Alexa+".

## The video

A 2:56 film. Most of it is labelled illustration of a 2030 home (Priya fills the bath while kneading dough, switches the house to
work mode, says good night and finds a leak, then asks what is using power while she sleeps); six labelled inserts are screen
recordings of the real add-on on the simulated house. Alexa's lines are the add-on's own sentences. The narration and voices are
AI-generated (Kokoro-82M, open weights); the music bed and effects are synthesised for the film. See `docs/DEMO.md`.

**Upload text** (YouTube or Vimeo, public; upload `leakhunt_film.srt` as English captions, off by default because the captions are
already in the picture):

* Title: Leak Hunt: Alexa+ finds where your water goes
* Description:

  > Leak Hunt is an Alexa+ add-on (an MCP server with an MCP App screen) for a home's water and power. It fills a bath to the
  > litre, switches the house's pipes off with "good night", and when water keeps moving with every tap off it runs a supervised
  > leak hunt that names a pipe only when the flow drops, comes back and drops again by the same amount. The same engine finds
  > what a home draws all night.
  >
  > The film shows illustrated scenes of a home in 2030 and screen recordings of the real add-on running on a simulated house,
  > through a web MCP client (the route the hackathon FAQ sets out). Narration and voices are AI-generated (Kokoro-82M); the music
  > and sound effects are synthesised for the film.
  >
  > Code (MIT): https://github.com/arulmuruganmcse2024-tech/leak-hunt · Controller conformance kit: https://github.com/arulmuruganmcse2024-tech/pipehub-conformance

## Checking it

`python3 -W ignore -m unittest discover -s tests -t .` (304 tests), `python3 -I tools/repro.py` (eight scenarios, byte-identical on
a second run), and the commands in `docs/VERIFICATION.md` (exhaustive and closed-loop fault injection, fuzzing, mutation testing, an
automatic-hunt fuzzer with eight deliberately broken services, a fill fuzzer over all three taps checked against each tap's true
litres with seven deliberately broken fills, a UI audit). In 300 random fills with no outside flow change (faults included: lost
meter data, a dead add-on, an unreachable controller, a jammed tap) no tap delivered more than the amount; with someone else's
water use changing just before or during the fill, at most 2.81 litres over, inside the documented limit. In 500 random simulated
homes for Power Hunt (with dead add-ons and stops) no water wording reached the customer, no must-stay-on plug was touched, no plug
that draws nothing was named, and every plug came back on; of 288 finished hunts with an always-on plug it named that plug in 272
and nothing in 16 (plugs drawing 4 to 15 % of the base load). One deliberately broken engine (M4) is not caught by the
bounded checks; `docs/VERIFICATION.md` explains why and what it does and does not show.

## What it does not claim

Filling by voice is not new: Kohler's PerfectFill fills a bath to a preset by voice and Delta's VoiceIQ pours measured amounts
through Alexa. Leak Hunt's fill controls volume only, never temperature. Not a better leak sensor: it investigates sustained flow above its configured threshold (assumed about 1 L/min, not yet measured),
and existing products state finer drip sensitivity. The isolation procedure itself is established (Moen tells owners to do it by
hand; a published patent document describes the valve sequence; Belkin's Phyn recognises fixtures). The contribution is the
supervised, repeatable, recorded form of it. Measuring a home's always-on power from its overnight minimum is established (Sense
does it), and per-device energy exists where a plug meters itself; Power Hunt's contribution is locating the draw behind an
ordinary, unmetered plug with replication. Sleep and away modes are familiar from phones and routines; what is added is the meter
check after the valves close. See `docs/PRIOR_ART.md` and `docs/HONEST_UNKNOWNS.md`.

## Built during the hackathon

Everything in the repository, and the film, was made during the submission period. Written by the entrant with AI coding
assistance (Claude Code). See `docs/DISCLOSURE.md`.

## Tools, APIs and SDKs used

MCP Python SDK 2.2.0, MCP specification 2025-11-25 (Streamable HTTP), the MCP Apps extension, OAuth 2.1 with PKCE, Playwright, and
Claude Code. What worked, what needs work, onboarding and whether I would use them again: `docs/PRODUCT_FEEDBACK.md`. Friction
log in the official fields (task, steps, expected and actual, severity, workaround, suggestion): `docs/FRICTION_LOG.md`.

## Open Source mini challenge: pipehub-conformance

**What I did.** Made a new open-source project, alongside this entry: a conformance checker for water valve controllers that
software is allowed to move. It turns Leak Hunt's controller contract into a short spec (`CONTRACT.md`, 20 numbered requirements)
and a checker that runs it against any controller over HTTP.

**How it works.** `pipehub_check.py URL --token T --pipes ... --outlets ...` runs 13 checks, standard library only. The central
ones send a lease and then nothing at all, and pass only if the controller puts the valve back by itself (pipes reopen, taps
close); the checker counts its own requests to prove it stayed silent. Others check that a tap never opens without a lease, that
the controller caps leases itself (300 s / 60 s), auth, 404s and malformed commands. `reference_hub.py` is a controller model that
passes, with nine `--broken` modes (ignores leases, opens a tap without a lease, no cap, no auth, wrong rest position, and more);
the self-tests run the full checker against each and require it to fail exactly the expected checks.

**Why it matters.** A lease turns "the software must never fail" into "the software may fail and the water still ends safe", and
it is what makes a spoken promise like "your controller turns it back on by itself within two minutes" true. Anyone building such
a controller (an ESP32 board, a commercial hub) can now test that promise before water flows. It has only been run against
software; the README lists what a pass does not prove and the bench tests that remain.

## Links

* Repository: https://github.com/arulmuruganmcse2024-tech/leak-hunt (public, MIT licence in the root)
* pipehub-conformance: https://github.com/arulmuruganmcse2024-tech/pipehub-conformance (its own public repository, MIT licence in the root)
* Video: https://youtu.be/2EoreksaOb4 (2:56, English, public on YouTube, no third-party logos or music)

---

## For me only (not part of the submission)

* [x] `LICENSE` names the copyright holder: Arul Murugan M (both repositories).
* [ ] Each repository is public and the host detects the MIT licence.
* [ ] Sensor bench session done and `docs/BENCH.md` filled in, or the sentence "not yet measured" kept everywhere it appears.
* [ ] Video watched by two people who have not seen the project; each can say in one sentence what it does.
* [ ] I confirm the "built during the hackathon" statement and the AI-assistance disclosure are true and complete.
* [ ] Product Feedback answer on the form agrees with `docs/PRODUCT_FEEDBACK.md`.
* [ ] Repository access: either public with the MIT licence shown at the top of the repository page, or private and shared with
      the judges' GitHub accounts listed on the rules page (re-check the list there before sharing).
* [ ] `pipehub-conformance/` published as its own public repository (MIT visible), its URL filled in above and in the mini
      challenge fields (contribution URL, project repository URL, GitHub username, description: the three paragraphs above).
* [ ] Track: Alexa+. Mini challenge: Open Source (one track prize and one mini challenge prize are possible).
* [ ] Optional, in your own words: an issue on the MCP Python SDK about the unchecked PKCE `code_challenge` (friction log entry
      2). Their CONTRIBUTING asks for an issue first and for AI assistance to be disclosed; I have not filed anything.
* [ ] Submitted by Fri 23 Oct 12:30 IST (deadline Sat 24 Oct 00:30 IST).
