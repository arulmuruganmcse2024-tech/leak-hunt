"""The outcome breakdown (tools/outcome_breakdown.py): every scenario lands in exactly one bucket, the buckets mean what their
names say, and the figures printed in docs/VERIFICATION.md are the ones in docs/check_outcomes.json.

Simulation only. These tests do not run a hunt; they test how finished runs are sorted and that the write-up matches the data.
"""
import copy, json, os, re, sys, unittest
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from tools.outcome_breakdown import FINDABLE, NOTHING, classify, summarise

DOCS = os.path.join(ROOT, "docs")


def sc(leaks, human="quick"):
    return dict(leaks=leaks, human=human)


def run(outcome, found=(), false_accusation=False, in_bound=True):
    return dict(outcome=outcome, found=list(found), false_accusation=false_accusation, in_bound=in_bound)


def written_figures_disagree(res: dict, text: str) -> list:
    """Every count in the JSON must appear in the VERIFICATION.md outcome table as '| label | count |'. Returns what does not."""
    bad = []
    for bound, label in (("in_bound", "in the proven bound"), ("outside_bound", "outside the bound")):
        for group in ("findable", "nothing_to_find"):
            rows = [("total", res[bound][group]["total"])] + list(res[bound][group]["buckets"].items())
            for name, n in rows:
                pat = r"\|\s*%s\s*\|\s*%s\s*\|\s*%s\s*\|\s*%d\s*\|" % (re.escape(label), re.escape(group), re.escape(name), n)
                if not re.search(pat, text):
                    bad.append((label, group, name, n))
    return bad


class Buckets(unittest.TestCase):
    def test_a_found_valve_with_a_leak_behind_it(self):
        self.assertEqual(classify(sc({"toilet": 2.0}), run("only_sources", ["toilet"])), ("findable", "named_a_valve_with_a_leak"))

    def test_a_dry_valve_is_its_own_bucket_and_never_counted_as_found(self):
        self.assertEqual(classify(sc({"toilet": 2.0}), run("only_sources", ["garden"], false_accusation=True)), ("findable", "named_a_dry_valve"))
        self.assertEqual(classify(sc({}), run("only_sources", ["garden"], false_accusation=True)), ("nothing_to_find", "named_a_valve"))

    def test_a_customer_who_never_answers_is_not_a_miss_of_the_engine(self):
        self.assertEqual(classify(sc({"toilet": 2.0}, "absent"), run("abandoned")), ("findable", "customer_never_answered"))
        self.assertEqual(classify(sc({"toilet": 2.0}, "quick"), run("abandoned")), ("findable", "other_nothing_named"))     # a present customer is

    def test_statements_about_where_the_water_is_not(self):
        for o in ("upstream_remainder", "no_flow_above_limit", "below_sensor_floor"):
            self.assertEqual(classify(sc({"toilet": 2.0}), run(o)), ("findable", "said_where_the_water_is_not"))

    def test_stopped_by_a_fault_or_the_customer(self):
        for o in ("aborted:pipe_moved", "aborted:telemetry_stale", "stopped"):
            self.assertEqual(classify(sc({"toilet": 2.0}), run(o)), ("findable", "stopped_by_the_house_or_a_fault"))

    def test_a_leak_below_the_floor_is_nothing_to_find(self):
        self.assertEqual(classify(sc({"toilet": 0.4}), run("below_sensor_floor")), ("nothing_to_find", "said_nothing_above_the_limit"))
        self.assertEqual(classify(sc({}), run("no_flow_above_limit")), ("nothing_to_find", "said_nothing_above_the_limit"))

    def test_every_bucket_name_is_one_the_tool_declares(self):
        cases = [(sc({"a": 2.0}), run(o, f)) for o in ("only_sources", "abandoned", "stopped", "partial", "upstream_remainder", "aborted:x")
                 for f in ([], ["a"])] + [(sc({}), run(o, f)) for o in ("no_flow_above_limit", "abandoned", "stopped", "partial") for f in ([], ["a"])]
        for s_, r_ in cases:
            g, b = classify(s_, r_)
            self.assertIn(b, FINDABLE if g == "findable" else NOTHING)

    def test_every_scenario_is_counted_exactly_once(self):
        rows = [dict(sc=sc({"a": 2.0}), r=run("only_sources", ["a"])), dict(sc=sc({}), r=run("no_flow_above_limit")),
                dict(sc=sc({"a": 2.0}, "absent"), r=run("abandoned")), dict(sc=sc({"a": 2.0}), r=run("stopped", in_bound=False)),
                dict(sc=sc({"a": 2.0}), r=run("only_sources", ["a"], in_bound=False))]
        s = summarise(rows)
        counted = sum(s[b][g]["total"] for b in ("in_bound", "outside_bound") for g in ("findable", "nothing_to_find"))
        in_buckets = sum(sum(s[b][g]["buckets"].values()) for b in ("in_bound", "outside_bound") for g in ("findable", "nothing_to_find"))
        self.assertEqual((counted, in_buckets, s["in_bound"]["total"], s["outside_bound"]["total"]), (5, 5, 3, 2))

    def test_a_classifier_that_filed_wrong_valves_as_found_would_be_caught(self):
        wrong = dict(sc=sc({"a": 2.0}), r=run("only_sources", ["b"], false_accusation=True))
        s = summarise([wrong])
        self.assertEqual(s["in_bound"]["findable"]["buckets"]["named_a_dry_valve"], 1)
        self.assertEqual(s["in_bound"]["findable"]["buckets"]["named_a_valve_with_a_leak"], 0)


class WriteUp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = json.load(open(os.path.join(DOCS, "check_outcomes.json")))
        cls.text = open(os.path.join(DOCS, "VERIFICATION.md"), encoding="utf-8").read()

    def test_the_numbers_in_the_write_up_are_the_numbers_in_the_data(self):
        self.assertEqual(written_figures_disagree(self.res, self.text), [])

    def test_a_changed_number_is_noticed(self):
        broken = copy.deepcopy(self.res)
        broken["in_bound"]["findable"]["buckets"]["named_a_valve_with_a_leak"] += 1
        self.assertTrue(written_figures_disagree(broken, self.text))

    def test_the_data_is_the_fuzzers_own_scenarios(self):
        z = json.load(open(os.path.join(DOCS, "check_auto_fuzz.json")))
        self.assertEqual(z["scenarios"], self.res["scenarios"])
        self.assertEqual(z["in_bound_scenarios"], self.res["in_bound"]["total"])
        found = (self.res["in_bound"]["findable"]["buckets"]["named_a_valve_with_a_leak"]
                 + self.res["in_bound"]["nothing_to_find"]["buckets"]["named_a_valve"])
        self.assertEqual(z["in_bound_with_a_finding"], found)

    def test_no_dry_valve_was_named_anywhere_in_the_300(self):
        for bound in ("in_bound", "outside_bound"):
            self.assertEqual(self.res[bound]["findable"]["buckets"]["named_a_dry_valve"], 0)
            self.assertEqual(self.res[bound]["nothing_to_find"]["buckets"]["named_a_valve"], 0)
        self.assertEqual(self.res["wrong_valve_outside_the_bound"], 0)


if __name__ == "__main__":
    unittest.main()
