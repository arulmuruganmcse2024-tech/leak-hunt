"""House modes ("Alexa, sleep mode", "away mode", "I'm home") on the simulated house, with a fake clock.

What these pin: a mode asks before it moves anything; it switches off exactly the pipes it says (sleep keeps the bathroom); its
valves have no lease, so they stay off however long the add-on is gone; it checks the meter afterwards and says honestly whether
water is still moving and where it can be; "I'm home" turns back on exactly what the mode turned off, never a pipe kept closed
after a leak or one that was off before; and modes, hunts and fills do not run over each other.

`mode_failures` is the check; each deliberately broken variant at the end must trip it.
"""
import os, sys, unittest, warnings
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import addon.modes as modes_mod
from addon.service import UserError
from tests.test_auto import start_auto
from tests.test_fill import make

warnings.simplefilter("ignore", ResourceWarning)


def positions(svc, clock):
    return {i: p.position for i, p in svc._hub("h").pipes(clock()).items()}


def run_mode(mode, sources=None, before=None, dead_for=0):
    """Ask, check nothing moved, say yes, wait for the meter check, optionally let the add-on die, and record what a checker needs."""
    svc, clock = make(sources)
    if before:
        before(svc, clock)
        clock.advance(25); svc.tick()
    pos0 = positions(svc, clock)
    ask = svc.house_mode("h", mode)
    moved_before_yes = {i for i, p in positions(svc, clock).items() if p != pos0[i]}
    on = svc.house_mode("h", mode, True) if ask.get("status") == "needs_confirm" else ask
    for _ in range(40):
        clock.advance(1); svc.tick()
        st = svc.house_mode("h")
        if st["status"] == "on":
            break
    hub = svc._hub("h")
    leases = {i: p.lease_left_s for i, p in hub.pipes(clock()).items()}
    if dead_for:
        for _ in range(dead_for):                                     # the add-on is gone: only the controller's own clock runs
            clock.advance(1); hub.expire(clock())
    return dict(svc=svc, clock=clock, ask=ask, on=on, status=st, pos0=pos0, moved_before_yes=moved_before_yes, leases=leases,
                after=positions(svc, clock), mode=mode)


def mode_failures(o):
    f = []
    if o["ask"].get("status") != "needs_confirm":
        f.append("M1 the mode switched pipes without asking first")
    if o["moved_before_yes"]:
        f.append(f"M2 pipes moved before the yes: {sorted(o['moved_before_yes'])}")
    closed = o["status"].get("closed", [])
    leased = [i for i in closed if o["leases"].get(i) is not None]
    if leased:
        f.append(f"M3 the mode's pipes have a lease, so the controller would reopen them: {leased}")
    reopened = [i for i in closed if o["after"].get(i) != "closed"]
    if reopened:
        f.append(f"M4 a mode's pipe came back on by itself: {reopened}")
    if o["status"].get("status") != "on" or o["status"].get("verified_lpm") is None:
        f.append("M5 no meter check after the mode switched")
    return f


class Modes(unittest.TestCase):
    def test_sleep_mode_asks_switches_the_rest_and_keeps_the_bathroom(self):
        o = run_mode("sleep mode")
        self.assertEqual(mode_failures(o), [])
        self.assertIn("Turn them off?", o["ask"]["speech"])
        self.assertEqual(sorted(o["status"]["closed"]), ["garden", "kitchen1", "kitchen2"])
        self.assertEqual((o["after"]["bath"], o["after"]["toilet"], o["after"]["shower"]), ("open", "open", "open"))
        self.assertTrue(o["status"]["still_flowing"])                  # the demo toilet leak is behind the bathroom, which stays on
        self.assertIn("The bathroom branch stays on in sleep mode, so it could be there", o["status"]["speech"])

    def test_away_mode_proves_the_house_is_dry(self):
        o = run_mode("away")
        self.assertEqual(mode_failures(o), [])
        self.assertEqual(sorted(o["status"]["closed"]), ["bath", "garden", "kitchen1", "kitchen2"])   # the bathroom branch covers its pipes
        self.assertFalse(o["status"]["still_flowing"])
        self.assertIn("no water is moving", o["status"]["speech"])

    def test_away_mode_says_when_water_moves_upstream_of_every_pipe(self):
        o = run_mode("away", before=lambda svc, clock: svc._source("h").set_ambient(1.6, clock()))
        self.assertEqual(mode_failures(o), [])
        self.assertTrue(o["status"]["still_flowing"])
        self.assertIn("upstream of all of them", o["status"]["speech"])

    def test_a_dead_add_on_leaves_the_mode_in_place(self):
        o = run_mode("away", dead_for=3600)
        self.assertEqual(mode_failures(o), [])
        self.assertEqual({o["after"][i] for i in o["status"]["closed"]}, {"closed"})

    def test_home_turns_back_on_only_what_the_mode_turned_off(self):
        svc, clock = make()
        svc.set_pipe("h", "garden tap", "off")                         # off before the mode: the customer's own decision
        clock.advance(3); svc.tick()
        svc.house_mode("h", "away", True)
        for _ in range(30):
            clock.advance(1); svc.tick()
        self.assertNotIn("garden", svc.house_mode("h")["closed"])
        p = svc.house_mode("h", "I'm home")
        self.assertEqual(p["status"], "off")
        pos = positions(svc, clock)
        self.assertEqual((pos["bath"], pos["kitchen1"], pos["kitchen2"], pos["garden"]), ("open", "open", "open", "closed"))
        self.assertIn("Away mode is off", p["speech"])

    def test_a_pipe_kept_closed_after_a_leak_stays_closed_after_home(self):
        svc, clock = make()
        svc.house_mode("h", "away", True)
        for _ in range(30):
            clock.advance(1); svc.tick()
        svc.store.set_latch("h", "kitchen2", clock(), None, 2.0)       # a leak found and kept closed in the meantime
        p = svc.house_mode("h", "home")
        self.assertEqual(positions(svc, clock)["kitchen2"], "closed")
        self.assertIn("kitchen pipe 2 stayed off because you asked me to keep it closed", p["speech"].replace("Kitchen", "kitchen"))

    def test_switching_from_sleep_to_away_and_back(self):
        svc, clock = make()
        svc.house_mode("h", "sleep", True)
        clock.advance(5); svc.tick()
        ask = svc.house_mode("h", "away")
        self.assertIn("also turns off bathroom branch", ask["speech"])
        svc.house_mode("h", "away", True)
        clock.advance(5); svc.tick()
        self.assertEqual(positions(svc, clock)["bath"], "closed")
        svc.house_mode("h", "sleep", True)                              # back to sleep: the bathroom comes back on
        clock.advance(5); svc.tick()
        pos = positions(svc, clock)
        self.assertEqual((pos["bath"], pos["kitchen1"]), ("open", "closed"))

    def test_words_people_use(self):
        for w, m in (("Good night", "sleep"), ("bedtime", "sleep"), ("I'm leaving for work", None), ("work mode", "away"),
                     ("vacation", "away"), ("I’m up", "home"), ("good morning", "home"), ("sleepmode", "sleep")):
            self.assertEqual(modes_mod.resolve_mode(w), m, w)

    def test_nothing_runs_over_anything_else(self):
        svc, clock = make()
        start_auto(svc)
        with self.assertRaises(UserError):
            svc.house_mode("h", "away")                                 # not during a hunt
        svc.stop_hunt("h")
        svc.start_fill("h", "bath", 50)
        with self.assertRaises(UserError):
            svc.house_mode("h", "away")                                 # not during a fill
        svc.stop_fill("h")
        for _ in range(30):
            clock.advance(1); svc.tick()
        svc.house_mode("h", "sleep", True)
        clock.advance(5); svc.tick()
        with self.assertRaises(UserError) as e:
            svc.start_hunt("h", mode="auto", consent=True)              # no hunt during a mode
        self.assertIn("Say 'I'm up' first", str(e.exception))
        with self.assertRaises(UserError) as e:
            svc.start_fill("h", "kitchen tap", 3)                       # the kitchen is off in sleep mode
        self.assertIn("Sleep mode has the kitchen pipe 1 off", str(e.exception))
        p = svc.start_fill("h", "bath", 20)                             # the bathroom stays on in sleep mode
        self.assertEqual(p["outlet"], "tub")

    def test_no_mode_while_a_keep_it_closed_question_is_open(self):
        svc, clock = make()
        svc.store.set_offer("h", "garden", clock(), clock() + 300, None, 2.0)
        with self.assertRaises(UserError) as e:
            svc.house_mode("h", "away")
        self.assertIn("Answer that first", str(e.exception))

    def test_status_without_a_mode_and_an_unknown_word(self):
        svc, clock = make()
        self.assertEqual(svc.house_mode("h")["status"], "off")
        with self.assertRaises(UserError):
            svc.house_mode("h", "party mode")


class BrokenVariants(unittest.TestCase):
    """Each broken mode must trip mode_failures; otherwise a green run proves nothing."""

    def caught(self, mutant, **kw):
        with mock.patch.object(modes_mod, "MUTANT", mutant):
            return mode_failures(run_mode(**kw))

    def test_a_mode_that_does_not_ask(self):
        self.assertTrue(any(x.startswith("M1") for x in self.caught("no_confirm", mode="away")))

    def test_a_mode_on_a_lease(self):
        f = self.caught("leased", mode="away", dead_for=600)
        self.assertTrue(any(x.startswith("M3") for x in f) and any(x.startswith("M4") for x in f), f)


if __name__ == "__main__":
    unittest.main()
