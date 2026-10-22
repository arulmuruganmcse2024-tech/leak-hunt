import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from huntkit import Config
from huntkit.baseline import analyse
from huntkit.sim import synth_history


class Baseline(unittest.TestCase):
    T_END = 1_800_000_000.0

    def test_clean_household_is_normal_and_does_not_overclaim(self):
        rs = synth_history(self.T_END, nights=10)
        rep = analyse(rs, Config())
        self.assertEqual(rep.verdict, "normal")
        self.assertIn("below what your sensor can read reliably", rep.text)
        self.assertNotIn("no leak", rep.text.lower())

    def test_new_overnight_flow_is_detected_with_onset(self):
        rs = synth_history(self.T_END, nights=10, leak_from_night=8, leak_lpm=2.0)
        rep = analyse(rs, Config())
        self.assertIn(rep.verdict, ("new_overnight_flow", "ongoing_overnight_flow"))
        self.assertGreaterEqual(rep.latest.min_lpm, 1.9)
        self.assertIsNotNone(rep.latest.sustained_from)

    def test_gaps_and_reboots_do_not_create_phantom_flow(self):
        rs = synth_history(self.T_END, nights=6)
        # drop a chunk of readings: coverage falls, the night must not be scored from the gap
        cut = [r for i, r in enumerate(rs) if not (500 < i % 2160 < 900)]
        rep = analyse(cut, Config())
        for n in rep.nights:
            if n.min_lpm is not None:
                self.assertLess(n.min_lpm, 0.5)

    def test_insufficient_data(self):
        rep = analyse(synth_history(self.T_END, nights=1), Config())
        self.assertEqual(rep.verdict, "insufficient_data")


if __name__ == "__main__":
    unittest.main()
