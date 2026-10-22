# The film (2:56.2; the rules allow under 3:00)

**Watch it:** https://youtu.be/2EoreksaOb4

The rules ask for an English video under three minutes that shows the project on its device **or a simulated Alexa+ experience**.
The official FAQ says participants cannot get the Alexa+ toolkit or simulator. The film therefore has two kinds of shot, and every
shot is labelled on screen:

* **Illustration · a home in 2030** (most of the film): original vector animation, made in code, of what Leak Hunt is for. Priya,
  her home, the smart speaker and the x-ray house are drawn for the film; the speaker is a generic design, not any company's
  product. These shots show intent, not a working installation.
* **Real add-on · simulated house** (six inserts): screen recordings of the dev host, a real MCP client, talking to the add-on
  over Streamable HTTP, on the simulated house and home. The numbers and sentences on these screens are the add-on's own.

Alexa's spoken lines are the add-on's own sentences (from `docs/repro_output.txt`, `tests/test_modes.py` and `tests/test_power.py`),
cut only at sentence or clause boundaries. The narration and all three voices (narrator, Priya, Alexa) are synthesised with
Kokoro-82M, an open-weight text-to-speech model (Apache-2.0); the Alexa voice is not Amazon's. The sound design and the background
pad are synthesised for the film from sine waves and noise: no third-party music, samples or logos.

The film is built from one timeline (`timeline.json`): the voices are synthesised first, the timeline is laid out from their
measured lengths, and the picture, the captions and the sound mix all read it, so they cannot drift apart. The production files
(scene code, voice lines, mixer, recorder) are kept outside the repository, next to it in the submission package.

## Shots

| Time | Screen | Voice (exactly as said; Alexa's lines are the add-on's own words) | What it proves |
| --- | --- | --- | --- |
| 0:00-0:11 | Illustration: a 2030 home at dawn, the house turns to x-ray: one meter, a pipe controller, a valve on every pipe | Narration: "This is a home in 2030. A meter watches every litre that comes in, and a small controller can switch every pipe." Narration: "Alexa+ runs the house. Meet Priya." | The setting: a home with a meter and a controller |
| 0:11-0:19 | Illustration: Priya kneading dough, hands covered in flour; a smart speaker on the counter lights up | Priya: "Alexa, fill the bath. 120 litres." Alexa: "Filling the bathtub with 120 litres. I'll stop it at 120." | Everyday voice use when your hands are busy |
| 0:19-0:34 | Illustration: the bath's valve opens, water fills the tub; a panel counts litres and shows the pipe controller's own timer | Narration: "The tap opens only on a timer inside the controller. Alexa keeps that timer equal to the litres still to go, so even if she goes silent, the tap closes on its own." | The controller's timer is the fill timer |
| 0:34-0:43 | The real add-on on the dev host, opening with the label "The real add-on, running on a simulated house": the fill screen ends at 120 litres and reports water moving elsewhere | Alexa: "The bathtub is filled: about 120 litres went in. Before I opened the tap, about 2.0 litres a minute was already moving somewhere else." | Real software, on a simulated house |
| 0:43-0:52 | Illustration: Priya at the front door with her bag; a card lists the pipes away mode switches off | Priya: "Alexa, work mode." Alexa: "Then I'll check the meter to make sure the water has really stopped. Turn them off?" Priya: "Yes." | A phone-style mode for pipes; it asks first |
| 0:52-1:00 | Illustration: x-ray house; four valves close one by one, the meter drops to zero | Alexa: "Away mode is on. I checked the meter: no water is moving above what your sensor can read." | It checks the meter, so off means off |
| 1:00-1:06 | The real add-on: the house-mode screen after the meter check, every switchable pipe off | Narration: "It's a phone's focus mode, for pipes. And because it checks the meter, off really means off." | Real software, on a simulated house |
| 1:06-1:10 | Illustration: bedroom at night; the lamp goes out | Priya: "Alexa, good night." | Bedtime |
| 1:10-1:41 | Illustration: sleep mode keeps the bathroom on, the meter still reads 2.0; next morning the pipes come back on and Alexa runs the test on the toilet valve: closed (drop), reopened (back), closed again (drop) | Alexa: "Sleep mode is on. But the meter still shows about 2.0 litres per minute. The bathroom branch stays on in sleep mode, so it could be there." Priya: "Alexa, I'm up. Find where it's going." Alexa: "I'll close your pipes one after another until the flow stops. I'll turn everything back on when I'm done, or the moment you say stop." Priya: "Go ahead." Narration: "She never trusts a single drop. A pipe is named only when the flow drops, comes back, and drops again by the same amount." | A self-checking mode finds the leak; replication before naming |
| 1:41-1:51 | The real add-on: the hunt's answer, a close-up of its own flow chart (Alexa closed the toilet valve, reopened it, closed it again: drop, back, drop), the keep-it-off question, Keep it closed; the toilet stays off until it is repaired | Alexa: "Confirmed: 2.0 litres per minute is flowing behind the toilet valve. Do you want it kept off until it has been checked?" Priya: "Yes, keep it off." | A separate yes before anything stays shut |
| 1:51-1:53 | Card: Same engine. A different meter. | (no voice) | The method is not about water |
| 1:53-2:03 | Illustration: living room at night, the TV's standby light; a chart of the lowest draw each night (62 W, then 108 W) | Priya: "Alexa, what's using power while we sleep?" Alexa: "Last night your home never drew less than 108 watts. Until two nights ago that was about 62 watts." | What runs all night |
| 2:03-2:14 | Illustration: the same house wired for power; plugs switch off one at a time, the meter drops 108 to 62 W; router and freezer locked | Narration: "Same engine, a different meter. Smart plugs switch off one at a time, each on a timer that turns it back on by itself. The router and the freezer are never touched." | Located and replicated; must-stay-on plugs never touched |
| 2:14-2:18 | The real add-on: Power Hunt's result screen | Alexa: "Confirmed: 46 watts is drawn behind the TV plug." | Real software, simulated home |
| 2:18-2:19 | Card: Why trust it? | (no voice) | From demo to test |
| 2:19-2:34 | The real dev host: try to fool it (fast-forward), Reveal: Leak Hunt right, the simplest rule wrong | Narration: "We tried to fool it. A tap that stops mid-measurement makes the simplest rule blame the wrong pipe. Leak Hunt names the right one. Across 600 hunts with one outside change, it never named a wrong valve." | The answer was not scripted into the engine |
| 2:34-2:40 | The real dev host: pull the plug; every closed pipe reopens on the controller's own timer (sped up) | Narration: "And if the add-on dies with pipes closed, the controller's own timers open every one of them." | A dead add-on ends at the safe state |
| 2:40-2:53 | Illustration: the home at dusk; title | Narration: "Today, Leak Hunt is working software for Alexa+, on a simulated house. It's open source, with over 300 tests. It's built for the homes that will have a valve on every pipe." | What is real today |
| 2:53-2:56 | End card: what it is, how it was tested, how it is shown (a web MCP client, the FAQ's route), a simulated home today | (no voice) | Honest labels |

`tests/test_demo_script.py` checks that the slots run back to back from 0:00, end by 2:57, and that no slot asks for more than
3.0 spoken words a second, counted over each slot. While actually talking, the synthesised voices average about 2.9 words a
second (425 words in 147 s of speech); Alexa's lines are the quickest, up to 3.6.

Do not say "proven", "the faulty pipe", "no leak", or that anything "wastes" money. Do not describe the pipe or plug controller as
built, or any meter as real. The 62 W and 46 W are the simulated home's numbers. Not everything comes back on by itself: a pipe or
plug the customer said to keep off, and a house mode's pipes until "I'm up", stay off because the customer asked.

## Before upload

* Under 3:00, English, the label visible on every shot.
* No third-party logos, brand or chip names, or copyrighted music.
* Nothing claims a result for a real house; no win, price, savings or demand claims.
* Captions: upload `leakhunt_film.srt` with the video.
