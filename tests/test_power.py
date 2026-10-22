"""Power Hunt ("Alexa, what's using power while we sleep?") on a simulated home, with a fake clock.

Simulation only: the energy meter, the smart plugs and the overnight history are models (addon/power.py). What these pin is that the
leak-hunt engine and its safety rules carry over unchanged to electricity: it asks before it switches anything; it never switches a
plug the household marked must-stay-on and never asks a person to; it names a plug only when the drop repeats, and only the right
one; every plug it switched off comes back on (by itself, on the controller's timer, if the add-on is gone); a "nothing here" bound
is never smaller than what the plug really draws; and no water wording ever reaches the customer.

`power_failures` is the check; each deliberately broken variant at the end must trip it.
"""
import os, random, re, sys, unittest, warnings
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import addon.power as power_mod
from addon import vocab
from addon.power import PowerService, DEMO_BASE_W, DEMO_EXTRA_W
from addon.service import HuntService, UserError, LEASE_S, OFFER_S
from addon.store import Store
from huntkit.core import ROOT
from tests.test_auto import FakeClock, human_step

warnings.simplefilter("ignore", ResourceWarning)
PROTECTED = {"router", "freezer"}


def make(sources=None, **kw):
    clock = FakeClock()
    svc = PowerService(Store(), clock, key=b"k", public_base_url="https://x.test")
    svc.ensure_demo_household("h", sources=sources, **kw)
    clock.advance(30); svc.tick()
    return svc, clock


def positions(svc, clock):
    return {i: p.position for i, p in svc._hub("h").pipes(clock()).items()}


def run_power(sources=None, mode="auto", dead_at=None, stop_at=None, max_s=1500, keep=False):
    """Ask, agree, say it's quiet, tick like the server (acting as the customer whenever the hunt needs hands), then let any offer
    run out. Records every presentation the customer could have heard or seen, and what a checker needs."""
    svc, clock = make(sources)
    said, errors = [], []

    def rec(fn, *a, **k):
        try:
            p = fn(*a, **k)
            said.append(p)
            return p
        except UserError as e:
            errors.append(str(e))
            return None

    rec(svc.water_status, "h"); rec(svc.night_flow, "h"); rec(svc.list_pipes, "h"); rec(svc.start_hunt, "h")
    if mode == "auto":
        rec(svc.start_hunt, "h", mode="auto")
        p = rec(svc.start_hunt, "h", mode="auto", consent=True)
    else:
        p = rec(svc.start_hunt, "h", mode="guided")
    hunt_id = p["hunt_id"]
    clock.advance(3); rec(svc.report, "h", hunt_id, "quiet")
    hub, dead = svc._hub("h"), False
    for i in range(max_s):
        clock.advance(1)
        if dead_at is not None and i >= dead_at:
            hub.expire(clock()); dead = True                     # the add-on is gone: only the plug controller's own timer runs
            if i >= dead_at + LEASE_S + 5:
                break
            continue
        if stop_at is not None and i == stop_at:
            rec(svc.stop_hunt, "h")
        svc.tick()
        p = rec(svc.hunt_status, "h", hunt_id)
        if p["status"] != "active":
            break
        if p["action"]["kind"] in ("quiet", "close", "open", "set_state") and (mode == "guided" or (p["auto"] and p["auto"]["needs_you"])):
            human_step(svc, "h", hunt_id, p)
            said.append(svc.hunt_status("h", hunt_id))
    final = said[-1]
    if keep:
        clock.advance(4); svc.tick()
        rec(svc.set_pipe, "h", final["found"][0], "off")         # "keep it off"
    if mode == "guided" and final.get("status") in ("done", "stopped"):   # the person does what the last line says
        m = re.search(r"You can switch (.+?) back on now\.", final["speech"])
        topo = svc._topology("h")
        for lab in re.split(r", | and ", m.group(1)) if m else []:
            node = svc._resolve_nodes(topo, [lab.removeprefix("the ")])[0]
            svc._source("h").set_closed(node, False, clock())    # by hand: the hunt is over, so nothing is reported
    if not dead:
        for _ in range(int(OFFER_S) + 10):                       # nobody answers "keep it off?": the offer runs out
            clock.advance(1); svc.tick()
        rec(svc.list_pipes, "h")
    hunt = svc.store.hunt(hunt_id)
    import json
    return dict(svc=svc, clock=clock, said=said, errors=errors, final=final, hunt_id=hunt_id, mode=mode, dead=dead,
                sources=dict(power_mod.DEMO_SOURCES if sources is None else sources), after=positions(svc, clock),
                acts=svc.store.actuations("h"), hunt_topology={r["id"] for r in hunt["topology"]})


def power_failures(o):
    f = []
    leftovers = [w for p in o["said"] for w in vocab.water_words(p)] + [e for e in o["errors"] if vocab.WATER_WORDS.search(e)]
    if leftovers:
        f.append(f"P1 water wording reached the customer: {leftovers[:3]}")
    switched = {a["pipe"] for a in o["acts"]} & PROTECTED
    asked = {n for p in o["said"] if p.get("kind") == "hunt" for n in (p["action"].get("close") or []) + (p["action"].get("open") or [])} & PROTECTED
    if switched or asked or (o["hunt_topology"] & PROTECTED):
        f.append(f"P2 a must-stay-on plug was switched, asked for, or put in the hunt: {sorted(switched | asked | (o['hunt_topology'] & PROTECTED))}")
    truth = {k for k, v in o["sources"].items() if k != ROOT and v > 0}
    named = set(o["final"].get("found") or [])
    if named - truth:
        f.append(f"P3 named a plug that draws nothing: {sorted(named - truth)} (true: {sorted(truth)})")
    kept = {l["pipe"] for l in o["svc"].store.latches("h")} | (named if o["mode"] == "guided" else set())   # theirs to keep off
    still_off = sorted(i for i, pos in o["after"].items() if i not in PROTECTED | kept and pos != "open")
    if still_off:
        f.append(f"P4 plugs still off after the hunt and the offer: {still_off}")
    for c in o["final"].get("claims") or []:
        if c["kind"] == "not_source" and c.get("hi") is not None and c["hi"] + 1e-6 < o["sources"].get(c["node"], 0.0):
            f.append(f"P5 '{c['node']}' excluded with a bound of {c['hi']:.1f} W but it draws {o['sources'][c['node']]:.1f} W")
    return f


class PowerHunt(unittest.TestCase):
    def test_status_says_what_runs_all_night(self):
        svc, clock = make()
        p = svc.water_status("h")
        self.assertEqual(p["utility"], "power")
        self.assertIn("never drew less than 108 watts", p["speech"])
        self.assertIn("that was about 62 watts, so about 46 watts more", p["speech"])
        self.assertIn(f"about {108 * 8.76:.0f} kilowatt-hours", p["speech"])
        self.assertIn("Want me to find what's drawing it?", p["speech"])
        self.assertEqual(vocab.water_words(p), [])

    def test_the_demo_hunt_finds_the_tv_plug_and_puts_everything_back(self):
        o = run_power()
        self.assertEqual(power_failures(o), [])
        p = o["final"]
        self.assertEqual((p["outcome"], p["found"]), ("partial", ["tv"]))
        self.assertIn("Confirmed: 46 watts is drawn behind the TV plug", p["speech"])
        self.assertIn("router plug and garage freezer plug, which I never switch", p["speech"])
        self.assertIn("Do you want it kept off?", p["speech"])
        self.assertTrue(o["said"][3]["speech"].endswith("I never switch the router plug or garage freezer plug."))

    def test_a_guided_hunt_never_asks_for_a_must_stay_on_plug(self):
        o = run_power(mode="guided")
        self.assertEqual(power_failures(o), [])
        self.assertEqual(o["final"]["found"], ["tv"])
        self.assertTrue(any("Switch off the TV plug (behind the TV stand), then tell me." in (p.get("speech") or "") for p in o["said"]))

    def test_a_dead_add_on_still_gets_every_plug_back_on(self):
        o = run_power(dead_at=30)
        self.assertEqual(power_failures(o), [])
        self.assertTrue(any(a["pipe"] == "tv" and a["want"] == "closed" for a in o["acts"]))   # it had switched one off

    def test_stop_switches_everything_back_on(self):
        o = run_power(stop_at=30)
        self.assertEqual(power_failures(o), [])
        self.assertIn("Stopped. I've switched back on every plug I switched off.", [p["speech"] for p in o["said"] if p.get("status") == "stopped"][0])

    def test_no_always_on_draw_behind_any_plug(self):
        o = run_power(sources={ROOT: 80.0})
        self.assertEqual(power_failures(o), [])
        self.assertEqual(o["final"]["outcome"], "upstream_remainder")
        self.assertIn("None of the plugs I can switch carries the always-on power (80 watts still drawn).", o["final"]["speech"])

    def test_must_stay_on_plugs_refuse_by_voice(self):
        svc, clock = make()
        with self.assertRaises(UserError) as e:
            svc.set_pipe("h", "freezer", "off")
        self.assertEqual(str(e.exception), "Garage freezer plug must stay on, so I never switch it.")

    def test_keep_it_off_and_switch_it_back_on(self):
        o = run_power(keep=True)
        self.assertEqual(power_failures(o), [])
        svc, clock = o["svc"], o["clock"]
        self.assertEqual(o["after"]["tv"], "closed")
        self.assertIn("I'll keep the TV plug off until you ask for it back.", o["said"][-2]["speech"])
        st = svc.water_status("h")
        self.assertIn("I'm keeping the TV plug off, as you asked.", st["speech"])
        self.assertNotIn("Want me to find", st["speech"])
        clock.advance(5)
        p = svc.set_pipe("h", "TV plug", "on", confirm=True)
        self.assertEqual(positions(svc, clock)["tv"], "open")
        self.assertEqual(vocab.water_words(p), [])

    def test_evidence_replays_and_reads_in_power_words(self):
        o = run_power()
        svc = o["svc"]
        pk = svc.evidence("h", o["hunt_id"])
        self.assertEqual(svc.verify_packet(pk), dict(hash_ok=True, hmac_ok=True, replay_matches=True))
        self.assertEqual(pk["assumptions"], power_mod.POWER_ASSUMPTIONS)
        page = svc.evidence_html(pk)
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", page, flags=re.S))
        self.assertEqual(vocab.WATER_WORDS.findall(text), [])
        self.assertIn("Power hunt evidence", page)

    def test_power_and_water_never_share_a_store_or_an_id(self):
        o = run_power(max_s=60)
        self.assertTrue(o["hunt_id"].startswith("p_"))
        water = HuntService(Store(), o["clock"], key=b"k")
        self.assertEqual(water.utility, "water")

    def test_random_homes(self):
        rng = random.Random(20261021)
        plugs = ["tv", "desk", "counter", "bedside"]
        for case in range(40):
            src = {ROOT: round(rng.uniform(20, 200), 1)}
            if rng.random() < 0.8:
                src[rng.choice(plugs)] = round(rng.uniform(15, 150), 1)
            with self.subTest(case=case, sources=src):
                o = run_power(sources=src, mode=rng.choice(["auto", "auto", "guided"]))
                self.assertEqual(power_failures(o), [])


class TheWords(unittest.TestCase):
    def test_numbers_become_watts(self):
        self.assertEqual(vocab.translate("Right now 108.0 litres per minute is flowing through your main line."),
                         "Right now 108 watts is being drawn from your supply.")
        self.assertEqual(vocab.translate("Flow 4.5 L/min"), "Power 4.5 W")

    def test_protocol_fields_are_never_rewritten(self):
        p = vocab.to_power(dict(kind="pipes", status="closed pipe", speech="the kitchen pipe is closed", pipe="kitchen pipe"))
        self.assertEqual((p["status"], p["pipe"], p["speech"]), ("closed pipe", "kitchen pipe", "the kitchen plug is off"))


class BrokenVariants(unittest.TestCase):
    """Each broken power hunt must trip power_failures; otherwise a green run proves nothing."""

    def caught(self, mutant, **kw):
        with mock.patch.object(power_mod, "MUTANT", mutant):
            return power_failures(run_power(**kw))

    def test_untranslated_words(self):
        self.assertTrue(any(x.startswith("P1") for x in self.caught("no_translate")))

    def test_must_stay_on_plugs_in_the_hunt(self):
        self.assertTrue(any(x.startswith("P2") for x in self.caught("protected_in_hunt", mode="guided")))


if __name__ == "__main__":
    unittest.main()
