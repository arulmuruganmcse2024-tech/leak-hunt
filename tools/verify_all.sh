#!/bin/sh
# Re-run the whole verification suite on the current code, in two parallel chains (about 45 minutes on two cores).
# Not included: the exhaustive depth-6 runs (see docs/VERIFICATION.md, about 3 minutes each) and the UI audit (needs a running dev server).
# Logs go to docs/; each check writes its docs/check_*.json.
cd "$(dirname "$0")/.." || exit 1
mkdir -p docs
rm -f docs/verify_all.done
(
  python3 -I tools/run_checks.py adaptive 2 T_leaks,K_leaks,no_leak > docs/run_adaptive_a.log 2>&1
  python3 -I tools/run_checks.py tight2 > docs/run_tight2.log 2>&1
  python3 -I tools/run_checks.py skeleton > docs/run_skeleton.log 2>&1
  python3 -I tools/run_checks.py mutants > docs/run_mutants.log 2>&1
  python3 -I tools/m4_analysis.py > docs/run_m4.log 2>&1
  python3 -I tools/auto_fuzz.py 300 0 docs/check_auto_fuzz.json > docs/run_auto_fuzz.log 2>&1
  for m in no_lease no_restore hand_valves stale_ignored stale_acts offer_unleased auto_latch no_remeasure; do
    AUTO_FUZZ_MUTANT=$m python3 -I tools/auto_fuzz.py 100 0 docs/check_auto_fuzz_mutant_$m.json > docs/run_auto_fuzz_mutant_$m.log 2>&1
  done
  AUTO_FUZZ_JITTER=1 python3 -I tools/auto_fuzz.py 300 0 docs/check_auto_fuzz_jitter.json > docs/run_auto_fuzz_jitter.log 2>&1
) &
(
  python3 -I tools/run_checks.py adaptive 2 both_leak,ambient_tap_then_stops > docs/run_adaptive_b.log 2>&1
  python3 -I tools/run_checks.py fuzz 100000 25 0 > docs/run_fuzz.log 2>&1
  python3 -I tools/challenge_search.py > docs/run_challenge_search.log 2>&1
  CHALLENGE_JITTER=1 python3 -I tools/challenge_search.py > docs/run_challenge_search_jitter.log 2>&1
  python3 -I tools/phase_grid.py > docs/run_phase_grid.log 2>&1
  python3 -I tools/fill_fuzz.py 300 0 docs/check_fill_fuzz.json > docs/run_fill_fuzz.log 2>&1
  for m in long_lease no_baseline ignore_change renew_stale no_backup no_stop_close no_recheck; do
    FILL_MUTANT=$m python3 -I tools/fill_fuzz.py 150 0 docs/check_fill_fuzz_mutant_$m.json > docs/run_fill_fuzz_mutant_$m.log 2>&1
  done
) &
wait
python3 -I tools/remeasure_diff.py > docs/run_remeasure_diff.log 2>&1
python3 -I tools/outcome_breakdown.py > docs/run_outcome_breakdown.log 2>&1
python3 -I tools/repro.py --check > docs/run_repro_check.log 2>&1
echo finished > docs/verify_all.done
