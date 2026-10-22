# Sensor bench session (about one hour) — NOT YET DONE

Today every sensor number in this project is an assumption: about 360 pulses per litre, about 1 L/min as the lowest flow it reads
reliably, a few percent run-to-run spread, 0.02 spurious pulses per second at zero flow (`huntkit/core.py`, `Config`). One
session with a measuring jug turns them into measurements. Until this page has a results row, every document keeps saying "not yet
measured".

**What this does not involve:** the pipe controller, any motorised valve, the device's main safety valve, or mains-powered kit
near water. It is a flow sensor, a tap, a jug and a stopwatch.

## You need

* the flow sensor fitted in a short hose or pipe run that ends in a jug or bucket, a drip tray under everything, low pressure
* a measuring jug (1 L or 2 L, with a clear mark) or kitchen scales (1 L of water is about 1 kg), a stopwatch
* a tap or hose with a valve you can set to a steady trickle
* the device's telemetry (its REST endpoint or serial log) so you can read the cumulative outlet volume before and after each run

## Steps

1. **Calibration, 5 runs.** Hold a steady flow of about 2 L/min and fill exactly 1.000 L. Write `calibration,1.000,<rise in the
   device's cumulative volume>,<seconds>`. Wait about 15 s after the water stops so the last telemetry sample has landed before you
   read the "after" value.
2. **Flow levels, at least 3 runs each.** Levels such as 0.3, 0.6, 1, 2 and 4 L/min. Set each by feel, then let the jug and stopwatch
   decide what it really was. Run each for 60 s or more (the device reports about every 10 s). Write
   `flow,<litres in the jug>,<rise in the device's volume>,<seconds>`.
3. **Zero flow, 10 minutes**, tap shut, nothing connected downstream moving. Write `zero,0,<rise in the device's volume>,600`.
4. **Optional, response time.** Close the tap and note how many seconds pass before the device's volume stops rising. The engine
   assumes it waits `settle_s` = 3 s after a reported action.

Save the rows as `session.csv` with the header `test,actual_l,reported_l,seconds`.

## Analyse

```sh
python3 -I tools/bench_fit.py session.csv --json docs/bench_result.json
```

It prints the device/jug ratio for calibration, the ratio and spread at each flow level, the zero-flow rate, and two **candidates**:
the lowest tested flow at which it and every higher tested flow stay within 20% of the jug with a spread of 15% or less
(at least 3 runs), and the largest spread among those. The criteria are mine and are flags on the command; argue with them if you
like, but write down which you used. It changes nothing.

## What to do with the result

| Result | Do |
| --- | --- |
| Floor at or below 1 L/min, calibration within 5% | Keep the wording. Replace "assumed about 1 L/min, not yet measured" with the measured figure and the date, in README, SUBMISSION and the video |
| Floor above 1 L/min | Set `Config.floor_lpm` in `huntkit/core.py` to the measured value (the spoken limit follows it), re-run `sh tools/verify_all.sh`, the unit tests and `tools/repro.py`, and narrow the claims everywhere to "above N L/min" |
| Calibration off by more than 5% | Fix the device's pulses-per-litre constant. Do not compensate inside the engine |
| No level passes | Say "the floor was not established"; do not keep a number you did not measure |
| Zero flow reads above 0.02 pulses/s | Raise `Config.noise_pps` to the measured value and re-run the same checks |

Do not claim a finer sensitivity than the table shows. Existing products state finer drip sensitivity (`docs/PRIOR_ART.md`).

## Results

| Date | Sensor and setup | Calibration (device/jug) | Floor (L/min) | Run-to-run spread | Zero-flow pulses/s | Criteria used |
| --- | --- | --- | --- | --- | --- | --- |
| not yet measured | | | | | | |
