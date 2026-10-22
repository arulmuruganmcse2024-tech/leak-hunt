"""tools/bench_fit.py: plain arithmetic on a bench session. It must not invent a floor the data does not support."""
import importlib.util
import os
import tempfile
import unittest

_spec = importlib.util.spec_from_file_location(
    "bench_fit", os.path.join(os.path.dirname(__file__), "..", "tools", "bench_fit.py"))
bench_fit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench_fit)


def rows(spec):
    """spec: list of (test, flow_lpm_or_None, seconds, device_over_jug) -> run rows."""
    out = []
    for test, lpm, secs, ratio in spec:
        actual = 0.0 if test == "zero" else (lpm * secs / 60.0 if lpm else 1.0)
        out.append(dict(test=test, actual_l=actual, reported_l=actual * ratio, seconds=float(secs)))
    return out


class BenchFit(unittest.TestCase):
    def test_floor_is_the_lowest_level_where_it_and_everything_above_pass(self):
        spec = []
        for lpm, ratios in ((0.5, (0.4, 1.9, 0.2)), (1.0, (0.95, 1.05, 1.0)), (2.0, (1.0, 0.98, 1.02)), (4.0, (1.01, 1.0, 0.99))):
            spec += [("flow", lpm, 60, r) for r in ratios]
        res = bench_fit.fit(rows(spec))
        self.assertEqual(res["candidates"]["floor_lpm"], 1.0)
        self.assertFalse(res["flow_levels"][0]["passes"])
        self.assertTrue(all(x["passes"] for x in res["flow_levels"][1:]))

    def test_a_good_level_below_a_bad_one_does_not_lower_the_floor(self):
        spec = []
        for lpm, ratios in ((0.5, (1.0, 1.0, 1.0)), (1.0, (0.4, 1.0, 1.9)), (2.0, (1.0, 1.0, 1.0))):
            spec += [("flow", lpm, 60, r) for r in ratios]
        self.assertEqual(bench_fit.fit(rows(spec))["candidates"]["floor_lpm"], 2.0)

    def test_too_few_runs_is_not_a_pass(self):
        res = bench_fit.fit(rows([("flow", 2.0, 60, 1.0), ("flow", 2.0, 60, 1.0)]))
        self.assertIsNone(res["candidates"]["floor_lpm"])
        self.assertIn("no flow level passed", res["candidates"]["note"])

    def test_calibration_and_zero_flow(self):
        spec = [("calibration", None, 60, 0.97)] * 4 + [("zero", None, 600, 0.0)]
        res = bench_fit.fit(rows(spec), ppl=360.0)
        self.assertAlmostEqual(res["calibration"]["mean_ratio"], 0.97, places=3)
        self.assertAlmostEqual(res["calibration"]["implied_ppl_if_device_uses_assumed"], 349.2, places=1)
        self.assertEqual(res["zero_flow"]["spurious_pulses_per_s"], 0.0)

    def test_csv_round_trip_and_bad_input(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.csv")
            with open(p, "w") as fh:
                fh.write("# a comment\ntest,actual_l,reported_l,seconds\ncalibration,1.0,0.98,40\nflow,2.0,1.98,60\n")
            r = bench_fit.load(p)
            self.assertEqual(len(r), 2)
            self.assertIn("calibration", bench_fit.render(bench_fit.fit(r)))
            with open(p, "w") as fh:
                fh.write("test,actual_l,reported_l,seconds\nbogus,1,1,1\n")
            with self.assertRaises(ValueError):
                bench_fit.load(p)
            with open(p, "w") as fh:
                fh.write("test,actual_l,reported_l,seconds\nflow,0,1,60\n")
            with self.assertRaises(ValueError):
                bench_fit.load(p)


if __name__ == "__main__":
    unittest.main()
