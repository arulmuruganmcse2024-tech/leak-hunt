"""Pull the plug: the add-on dies with pipes closed and the simulated pipe controller reopens them on its own timer.

Dev host only, simulation only: the add-on's loop is silenced (addon.challenge.Challenge), the controller is
addon.actuators.VirtualPipeHub and the clock is fake. What this pins is what the dev-host screen shows a judge: with the add-on
gone, nothing of it runs and every pipe it had closed is open again within the lease, reopened by the controller's own timer.
It does not show that a real controller behaves this way; docs/PIPE_HUB.md says what a real one must do.

The check is `unplugged_failures`. Each deliberately broken variant below must trip it, so a green result means something.
"""
import asyncio, json, os, sys, threading, time, unittest, urllib.error, urllib.request, warnings
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import uvicorn
from addon.challenge import Challenge
from addon.server import build_server
from addon.service import HuntService, LEASE_S
from addon.store import Store
from huntkit import Config
from tests.test_auto import FakeClock, human_step, make, physical, start_auto
from tests.test_server_http import free_port

warnings.simplefilter("ignore", ResourceWarning)


def run_plug(variant=None, read_every_second=False, outage_s=None):
    """A hunt with at least one pipe closed; the add-on is killed; time runs on. Returns what happened."""
    svc, clock = make()
    ch = Challenge(svc, "h")
    ch.new_round(11)
    clock.advance(1)
    hid = start_auto(svc)["hunt_id"]
    hub = svc._hub("h")
    for i in range(400):                                          # until the hunt has pipes closed, as in the demo
        clock.advance(1)
        svc.tick()
        v = svc.hunt_status("h", hid)
        if v["auto"] and v["auto"]["needs_you"]:
            human_step(svc, "h", hid, v)
        closed = [p for p, s in hub.pipes(clock()).items() if s.position == "closed"]
        if closed and i > 60:
            break
    out = dict(closed_at_kill=sorted(closed), lease_until=dict(hub.lease_until), t_kill=clock(), hunt=hid,
               actuations_before=svc.store.count_actuations(hid), runs={}, views=[])
    if variant == "controller_ignores_its_lease":
        hub.expire = lambda now: None                             # a controller that never reopens anything
    ch.act("addon_down")
    if variant == "kill_does_not_stop_the_add_on":
        svc.tick = ch._real_tick                                  # the "kill" leaves the add-on's heartbeat running
    seen = dict(drive=0, poll=0)
    for name, key in (("_drive", "drive"), ("_poll", "poll")):
        orig = getattr(svc, name)
        def counted(*a, _orig=orig, _key=key, **k):
            seen[_key] += 1
            return _orig(*a, **k)
        setattr(svc, name, counted)
    for _ in range(int(outage_s if outage_s is not None else LEASE_S + 8)):
        clock.advance(1)
        svc.tick()
        if read_every_second:
            out["views"].append(ch.controller())
        for p, s in hub.pipes(clock()).items():
            out["runs"][p] = out["runs"].get(p, 0) + (1 if s.position == "closed" else 0)
    out.update(addon_runner_calls=seen["drive"], addon_polls=seen["poll"], lapsed=[(p, u) for p, u in hub.lapsed],
               actuations_after=svc.store.count_actuations(hid), closed_at_end=physical(svc), svc=svc, ch=ch, clock=clock, hub=hub)
    return out


def unplugged_failures(o):
    """Everything that must hold after the add-on has been silent for longer than the lease. Empty list = it held."""
    f = []
    if o["addon_runner_calls"] or o["addon_polls"]:
        f.append("the add-on's own code ran while it was supposed to be dead (%d runner calls, %d polls)" % (o["addon_runner_calls"], o["addon_polls"]))
    if o["actuations_after"] != o["actuations_before"]:
        f.append("a valve command was issued while the add-on was dead")
    if not o["closed_at_kill"]:
        f.append("no pipe was closed at the moment of the kill, so nothing was shown")
    lapsed = dict(o["lapsed"])
    for p in o["closed_at_kill"]:
        if p not in lapsed:
            f.append("%s was never reopened by the controller's own timer" % p)
        elif lapsed[p] != o["lease_until"][p]:
            f.append("%s reopened at %s, not when its lease ended (%s)" % (p, lapsed[p], o["lease_until"][p]))
    if o["closed_at_end"]:
        f.append("pipes still closed after the lease: %s" % o["closed_at_end"])
    longest = max(o["runs"].values()) if o["runs"] else 0
    if longest > LEASE_S + 1:
        f.append("a pipe stayed closed for %d s, longer than the %d s lease" % (longest, LEASE_S))
    return f


class PullThePlug(unittest.TestCase):
    def test_with_the_add_on_dead_every_closed_pipe_reopens_by_the_controllers_own_timer(self):
        o = run_plug()
        self.assertEqual(unplugged_failures(o), [])
        self.assertGreaterEqual(len(o["closed_at_kill"]), 1)
        self.assertLessEqual(max(o["runs"].values()), LEASE_S)       # no pipe was ever closed longer than one lease
        left = {p: round(t - o["t_kill"], 1) for p, t in o["lease_until"].items() if p in o["closed_at_kill"]}
        self.assertEqual({p: round(u - o["t_kill"], 1) for p, u in o["lapsed"]}, left)        # each reopened exactly when its timer said

    def test_the_controller_view_reports_what_the_controller_itself_knows(self):
        o = run_plug(read_every_second=True)
        first, last = o["views"][0], o["views"][-1]
        self.assertEqual(first["name"], "Pipe controller (simulated)")
        self.assertTrue(first["addon_down"])
        self.assertTrue(all(r["lease_left_s"] is not None for r in first["pipes"] if r["position"] == "closed"))
        self.assertEqual(sorted(r["id"] for r in last["lapsed_since_plug_pulled"]), o["closed_at_kill"])
        self.assertTrue(all(r["position"] == "open" for r in last["pipes"]))
        self.assertGreater(last["silent_s"], LEASE_S)

    def test_looking_at_the_controller_does_not_change_what_it_does(self):
        a, b = run_plug(read_every_second=False), run_plug(read_every_second=True)
        self.assertEqual(a["lapsed"], b["lapsed"])
        self.assertEqual(a["runs"], b["runs"])
        self.assertEqual(a["closed_at_end"], b["closed_at_end"])

    # ---- each broken variant must trip the check; otherwise a green result proves nothing
    def test_broken_variant_a_controller_that_ignores_its_lease_is_caught(self):
        f = unplugged_failures(run_plug("controller_ignores_its_lease"))
        self.assertTrue(any("never reopened" in x for x in f), f)
        self.assertTrue(any("still closed" in x for x in f), f)

    def test_broken_variant_a_kill_that_leaves_the_add_on_running_is_caught(self):
        f = unplugged_failures(run_plug("kill_does_not_stop_the_add_on"))
        self.assertTrue(any("add-on's own code ran" in x for x in f), f)

    def test_the_check_notices_when_there_was_nothing_to_show(self):
        o = run_plug()
        o["closed_at_kill"] = []
        self.assertTrue(any("nothing was shown" in x for x in unplugged_failures(o)))

    # ---- after the outage
    def test_when_the_add_on_comes_back_it_stops_rather_than_guess(self):
        o = run_plug()
        svc, ch, clock = o["svc"], o["ch"], o["clock"]
        ch.act("addon_up")
        v = None
        for _ in range(200):
            clock.advance(1)
            svc.tick()
            v = svc.hunt_status("h", o["hunt"])
            if v["status"] != "active":
                break
        self.assertEqual(v["status"], "aborted")
        self.assertTrue(v["outcome"].startswith("aborted:pipe_moved:"), v["outcome"])
        self.assertIn("I stopped rather than guess", v["speech"])
        self.assertEqual(physical(svc), [])

    def test_a_new_round_brings_the_add_on_back(self):
        o = run_plug()
        svc, ch, clock = o["svc"], o["ch"], o["clock"]
        self.assertTrue(ch.state()["addon_down"])
        ch.new_round(12)
        self.assertFalse(ch.state()["addon_down"])
        runs = []
        real = ch._real_tick
        ch._real_tick = lambda: (runs.append(1), real())
        clock.advance(1)
        svc.tick()
        self.assertEqual(len(runs), 1)

    def test_pulling_it_twice_keeps_the_first_time(self):
        o = run_plug(outage_s=5)
        ch = o["ch"]
        t = ch._t_down
        ch.act("addon_down")
        self.assertEqual(ch._t_down, t)


class OverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for devhost in (False, True):
            clock = FakeClock()
            svc = HuntService(Store(), clock, Config(), key=b"k", public_base_url="http://127.0.0.1")
            svc.ensure_demo_household("default", control=True)
            app = build_server(svc, devhost=devhost).streamable_http_app(json_response=True)
            port = free_port()
            srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
            th = threading.Thread(target=srv.run, daemon=True)
            th.start()
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    urllib.request.urlopen(base + "/healthz", timeout=0.5); break
                except Exception:
                    time.sleep(0.1)
            cls.servers.append((srv, th, base, svc))

    @classmethod
    def tearDownClass(cls):
        for srv, th, _, _ in cls.servers:
            srv.should_exit = True
            th.join(timeout=5)

    @staticmethod
    def call(base, path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, {}

    def test_the_route_does_not_exist_unless_the_dev_host_is_on(self):
        self.assertEqual(self.call(self.servers[0][2], "/challenge/hub")[0], 404)

    def test_the_controller_view_and_the_plug_over_http(self):
        base = self.servers[1][2]
        self.assertEqual(self.call(base, "/challenge/new", {"seed": 11})[0], 200)
        st, ctl = self.call(base, "/challenge/hub")
        self.assertEqual(st, 200)
        self.assertEqual((ctl["name"], ctl["addon_down"], ctl["reachable"], ctl["lease_s"]), ("Pipe controller (simulated)", False, True, LEASE_S))
        self.assertEqual(len(ctl["pipes"]), 6)
        st, s = self.call(base, "/challenge/chaos", {"action": "addon_down"})
        self.assertEqual((st, s["addon_down"]), (200, True))
        self.assertTrue(self.call(base, "/challenge/hub")[1]["addon_down"])
        st, s = self.call(base, "/challenge/chaos", {"action": "addon_up"})
        self.assertEqual((st, s["addon_down"]), (200, False))

    def test_a_request_from_another_machine_is_refused(self):
        class R:
            client = type("C", (), {"host": "10.1.2.3"})()
            method = "GET"
        mcp = build_server(self.servers[1][3], devhost=True)
        route = [r for r in mcp.streamable_http_app(json_response=True).routes if getattr(r, "path", "") == "/challenge/hub"][0]
        self.assertEqual(asyncio.run(route.endpoint(R())).status_code, 403)


if __name__ == "__main__":
    unittest.main()
