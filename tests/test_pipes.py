"""Naming pipes and resolving what a person says back to a pipe (huntkit.pipes), plus its use by the service."""
import sys, unittest, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from huntkit.core import Node, Topology, ROOT
from huntkit.pipes import resolve, name_pipes, words, normalise, describe_pipes, join_or
from huntkit.sim import demo_topology


class Words(unittest.TestCase):
    def test_numbers_in_every_spoken_form(self):
        for phrase in ("pipe 2", "pipe two", "second pipe", "number 2", "#2", "2nd pipe", "pipe2"):
            self.assertIn("2", words(phrase), phrase)

    def test_plural_flag(self):
        self.assertTrue(normalise("both kitchen pipes")[1])
        self.assertTrue(normalise("all the pipes")[1])
        self.assertFalse(normalise("kitchen pipe 1")[1])


class Resolve(unittest.TestCase):
    def setUp(self):
        self.t = demo_topology()

    def one(self, phrase, node):
        m = resolve(self.t, phrase)
        self.assertEqual((m.kind, m.nodes), ("one", [node]), phrase)
        return m

    def test_numbered_pipes_resolve_in_any_wording(self):
        for ph in ("kitchen pipe 1", "kitchen pipe one", "the first kitchen pipe", "kitchen 1", "the kitchen pipe number one"):
            self.one(ph, "kitchen1")
        for ph in ("kitchen pipe 2", "kitchen pipe two", "the second kitchen pipe", "kitchen 2"):
            self.one(ph, "kitchen2")

    def test_bare_room_with_two_pipes_asks_and_never_guesses(self):
        m = resolve(self.t, "kitchen")
        self.assertEqual((m.kind, m.nodes), ("many", ["kitchen1", "kitchen2"]))
        self.assertFalse(m.all_requested)
        self.assertIn("kitchen pipe 1 or kitchen pipe 2", m.question)

    def test_all_and_both_pick_the_whole_room(self):
        for ph in ("all kitchen pipes", "both kitchen pipes", "kitchen pipes"):
            m = resolve(self.t, ph)
            self.assertEqual((m.kind, m.nodes, m.all_requested), ("many", ["kitchen1", "kitchen2"], True), ph)

    def test_everything_means_every_pipe_but_both_alone_does_not(self):
        self.assertEqual(len(resolve(self.t, "everything").nodes), len(self.t.nodes))
        self.assertEqual(resolve(self.t, "both").kind, "none")       # both of what?

    def test_aliases(self):
        self.one("sink", "kitchen1")
        self.one("dishwasher", "kitchen2")

    def test_a_branch_valve_wins_over_the_pipes_behind_it(self):
        self.one("bathroom", "bath")
        self.one("toilet", "toilet")
        self.one("bathroom toilet", "toilet")

    def test_garden_tap_keeps_its_noun(self):
        self.one("garden", "garden")
        self.one("garden tap", "garden")

    def test_unknown_numbers_and_rooms_are_not_guessed(self):
        m = resolve(self.t, "kitchen pipe 3")
        self.assertEqual(m.kind, "none")
        self.assertIn("kitchen pipe 1 and kitchen pipe 2", m.question)
        self.assertEqual(resolve(self.t, "basement").kind, "none")
        self.assertEqual(resolve(self.t, "").kind, "none")

    def test_a_bare_number_only_resolves_when_it_is_unique(self):
        self.one("pipe 2", "kitchen2")
        rows = name_pipes([{"room": "kitchen"}, {"room": "kitchen"}, {"room": "laundry"}, {"room": "laundry"}])
        t = Topology([Node(**{k: (tuple(v) if k == "aliases" else v) for k, v in r.items()}) for r in rows])
        m = resolve(t, "pipe 2")
        self.assertEqual((m.kind, len(m.nodes)), ("many", 2))
        self.assertIn("Which one", m.question)

    def test_one_misheard_word_is_repaired_and_flagged(self):
        m = resolve(self.t, "kitchn pipe 1")
        self.assertEqual((m.kind, m.nodes, m.fuzzy), ("one", ["kitchen1"], True))
        self.assertEqual(resolve(self.t, "zzzz pipe 1").kind, "none")

    def test_two_misheard_words_are_not_guessed(self):
        self.assertEqual(resolve(self.t, "kitchn pip 1 dishwaser").kind, "none")

    def test_ids_never_hijack_spoken_words(self):
        # the old demo id "kitchen" used to match the spoken word "kitchen" ahead of the customer's names
        t = Topology([Node("kitchen", "sink", ROOT, room="kitchen"), Node("k2", "dishwasher", ROOT, room="kitchen")])
        m = resolve(t, "kitchen")
        self.assertEqual(m.kind, "many")

    def test_pool_limits_the_candidates(self):
        m = resolve(self.t, "kitchen", pool=["kitchen1", "garden"])
        self.assertEqual((m.kind, m.nodes), ("one", ["kitchen1"]))
        self.assertEqual(resolve(self.t, "toilet", pool=["kitchen1"]).kind, "none")

    def test_describe_and_join(self):
        d = describe_pipes(self.t)
        self.assertIn("kitchen: kitchen pipe 1, kitchen pipe 2", d)
        self.assertEqual(join_or(["a", "b", "c"], "and"), "a, b and c")
        self.assertEqual(join_or(["a", "b"]), "a or b")


class NamePipes(unittest.TestCase):
    def test_two_unnamed_kitchen_pipes_are_numbered(self):
        rows = name_pipes([{"room": "kitchen"}, {"room": "kitchen"}])
        self.assertEqual([r["label"] for r in rows], ["kitchen pipe 1", "kitchen pipe 2"])
        self.assertEqual([r["number"] for r in rows], [1, 2])
        self.assertEqual(len({r["id"] for r in rows}), 2)

    def test_a_single_pipe_in_a_room_is_not_numbered(self):
        rows = name_pipes([{"room": "laundry"}])
        self.assertEqual((rows[0]["label"], rows[0]["number"]), ("laundry pipe", 0))

    def test_names_win_and_explicit_numbers_are_kept(self):
        rows = name_pipes([{"room": "bathroom", "name": "toilet"}, {"room": "kitchen", "number": 2}, {"room": "kitchen"}])
        self.assertEqual(rows[0]["label"], "bathroom toilet")
        self.assertIn("toilet", rows[0]["aliases"])
        self.assertEqual([rows[1]["number"], rows[2]["number"]], [2, 1])

    def test_ids_are_unique_even_when_labels_repeat(self):
        rows = name_pipes([{"label": "garden tap"}, {"label": "garden tap"}])
        self.assertEqual(len({r["id"] for r in rows}), 2)

    def test_a_row_with_nothing_to_call_it_is_refused(self):
        with self.assertRaises(ValueError):
            name_pipes([{"prior": 1}])

    def test_round_trip_through_topology_json(self):
        t = demo_topology()
        t2 = Topology.from_json(t.to_json())
        self.assertEqual(t2.to_json(), t.to_json())
        old = [{k: v for k, v in r.items() if k not in ("room", "number", "aliases", "protect")} for r in t.to_json()]
        self.assertEqual(len(Topology.from_json(old).nodes), len(t.nodes))      # households saved before pipes had names


class ServiceUse(unittest.TestCase):
    """A repaired guess is confirmed before anything moves."""

    def test_misheard_word_asks_first(self):
        from tests.test_service import FakeClock
        from addon.service import HuntService
        from addon.store import Store
        from huntkit.core import Config
        svc = HuntService(Store(), FakeClock(), Config(), key=b"k")
        svc.ensure_demo_household("default")
        r = svc.set_pipe("default", "kitchn pipe 1", "off")
        self.assertEqual((r["kind"], r["reason"]), ("needs_confirm", "heard"))
        self.assertIn("Did you mean kitchen pipe 1", r["speech"])
        self.assertEqual(svc._source("default").physical_closed(), set())
        svc.clock.advance(3)
        r = svc.set_pipe("default", "kitchn pipe 1", "off", confirm=True)
        self.assertEqual(svc._source("default").physical_closed(), {"kitchen1"})


if __name__ == "__main__":
    unittest.main()
