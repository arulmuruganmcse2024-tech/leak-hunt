"""The simulated house runs on a whole-second timeline: one tick = exactly one simulated second, at any time-lapse factor."""
import os
import sys
import threading
import time
import unittest
import warnings
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from addon.server import SteppedClock, service_from_env, start_ticker

warnings.simplefilter("ignore", ResourceWarning)


class Stepped(unittest.TestCase):
    def test_time_moves_only_when_it_is_advanced_and_by_exactly_what_was_asked(self):
        c = SteppedClock(1_000.0)
        a = c()
        time.sleep(0.05)
        self.assertEqual(c(), a)
        c.advance(1.0); c.advance(1.0)
        self.assertEqual(c(), 1_002.0)

    def test_the_simulated_house_gets_the_stepped_clock_at_any_speed(self):
        for speed in ("1", "6", "30"):
            env = {"LEAKHUNT_MODE": "virtual", "LEAKHUNT_SPEED": speed, "LEAKHUNT_DB": ":memory:", "LEAKHUNT_KEY": "k"}
            with mock.patch.dict(os.environ, env, clear=False):
                svc = service_from_env()
            self.assertIsInstance(svc.clock, SteppedClock)
            self.assertEqual(svc.time_scale, float(speed))
            self.assertEqual(svc.clock() % 1.0, 0.0)                     # whole seconds from the start

    def test_each_beat_of_the_ticker_is_exactly_one_simulated_second(self):
        env = {"LEAKHUNT_MODE": "virtual", "LEAKHUNT_SPEED": "30", "LEAKHUNT_DB": ":memory:", "LEAKHUNT_KEY": "k"}
        with mock.patch.dict(os.environ, env, clear=False):
            svc = service_from_env()
        t0, stop, beats = svc.clock(), threading.Event(), []
        real_tick = svc.tick
        svc.tick = lambda: (beats.append(svc.clock()), real_tick())
        th = start_ticker(svc, 0.005, stop)
        deadline = time.time() + 5
        while len(beats) < 5 and time.time() < deadline:
            time.sleep(0.01)
        stop.set(); th.join(2)
        self.assertGreaterEqual(len(beats), 5)
        self.assertEqual(beats[:5], [t0 + 1, t0 + 2, t0 + 3, t0 + 4, t0 + 5])

    def test_a_real_device_never_runs_on_a_time_lapse(self):
        env = {"LEAKHUNT_MODE": "device", "LEAKHUNT_SPEED": "6", "LEAKHUNT_DB": ":memory:", "LEAKHUNT_KEY": "k",
               "LEAKHUNT_SENSOR_URL": "http://x", "LEAKHUNT_SENSOR_ID": "d"}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(SystemExit):
                service_from_env()


if __name__ == "__main__":
    unittest.main()
