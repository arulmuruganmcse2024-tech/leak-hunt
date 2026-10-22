#!/usr/bin/env python3
"""Run the verification suite. Usage: run_checks.py exhaustive|skeleton|fuzz|tightness|mutants [args]"""
import json, sys, time, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from huntkit.check import *
from huntkit.core import Config
from huntkit.mutants import MUTANTS

TRUTHS = {
    "T_leaks": ({"T": 2.0}, 0.0),
    "K_leaks": ({"K": 2.0}, 0.0),
    "both_leak": ({"T": 2.0, "K": 1.5}, 0.0),
    "no_leak": ({}, 0.0),
    "ambient_tap_then_stops": ({}, 2.0),   # truth: no source, someone's tap running at the start
}

def out(name, obj):
    path = os.path.join(os.path.dirname(__file__), "..", "docs", f"check_{name}.json")
    json.dump(obj, open(path, "w"), indent=1)
    print(json.dumps(obj)[:2000])

def main():
    mode = sys.argv[1]
    topo, cfg = check_topology(), Config()
    if mode == "exhaustive":
        depth = int(sys.argv[2]); keys = sys.argv[3].split(",")
        res = {}
        for k in keys:
            src, ex = TRUTHS[k]
            t = time.time()
            st = explore(topo, src, cfg, depth, max_faults=2, extra0=ex)
            res[k] = dict(depth=depth, nodes=st.nodes, violations=len(st.violations), first=st.violations[:3],
                          confirmed_reached=st.confirmed_reached, only_sources_reached=st.only_sources_reached,
                          aborted_reached=st.aborted_reached, seconds=round(time.time() - t, 1))
        out(f"exhaustive_d{depth}_{keys[0]}", res)
    elif mode == "skeleton":
        res = {}
        for k, (src, ex) in TRUTHS.items():
            script = autopilot_script(topo, src, cfg, extra0=ex)
            t = time.time()
            st = skeleton_faults(topo, src, cfg, script, max_faults=2, extra0=ex)
            res[k] = dict(script_len=len(script), schedules_nodes=st.nodes, violations=len(st.violations), first=st.violations[:3],
                          confirmed_reached=st.confirmed_reached, only_sources_reached=st.only_sources_reached,
                          aborted_reached=st.aborted_reached, seconds=round(time.time() - t, 1))
        out("skeleton", res)
    elif mode == "fuzz":
        n = int(sys.argv[2]); depth = int(sys.argv[3]); seed = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        t = time.time()
        from huntkit.sim import demo_topology
        res = {}
        for name, tp in (("check_topology", topo), ("demo_topology", demo_topology())):
            st = fuzz(tp, cfg, n, depth, seed=seed)
            res[name] = dict(schedules=n, depth=depth, prefix_evals=st.nodes, violations=len(st.violations), first=st.violations[:3],
                             confirmed_reached=st.confirmed_reached, only_sources_reached=st.only_sources_reached,
                             aborted_reached=st.aborted_reached)
        res["seconds"] = round(time.time() - t, 1)
        out(f"fuzz_{seed}", res)
    elif mode == "tightness":
        script = autopilot_script(topo, {"T": 2.0}, cfg)   # a full hunt with replication
        res = dict(script=script, result=tightness(topo, cfg, script))
        out("tightness", res)
    elif mode == "adaptive":
        k = int(sys.argv[2]); keys = sys.argv[3].split(",") if len(sys.argv) > 3 else list(TRUTHS)
        res = {}
        for key in keys:
            src, ex = TRUTHS[key]
            t = time.time()
            st = adaptive_faults(topo, src, cfg, max_faults=k, extra0=ex)
            res[key] = dict(max_faults=k, nodes=st.nodes, violations=len(st.violations), first=st.violations[:3],
                            confirmed_reached=st.confirmed_reached, only_sources_reached=st.only_sources_reached,
                            aborted_reached=st.aborted_reached, seconds=round(time.time() - t, 1))
            print(json.dumps({key: res[key]})[:600], flush=True)
        out(f"adaptive_k{k}_{keys[0]}", res)
    elif mode == "tight2":
        res = {}
        for key, (src, ex) in {"no_source_ambient": ({}, 2.0), "T_leaks_ambient_off": ({"T": 2.0}, 0.0),
                               "K_leaks_ambient_on": ({"K": 2.0}, 2.0)}.items():
            t = time.time()
            res[key] = tightness_adaptive(topo, cfg, src, extra0=ex, k_max=3)
            res[key]["seconds"] = round(time.time() - t, 1)
            print(json.dumps({key: res[key]})[:900], flush=True)
        out("tightness_adaptive", res)
    elif mode == "mutants":
        res = {}
        for name, cls in MUTANTS.items():
            found, nodes = None, 0
            def scripts(src, ex):
                return autopilot_script(topo, src, cfg, extra0=ex)
            stages = [
                ("closed-loop faults, <=1 fault", lambda src, ex: adaptive_faults(topo, src, cfg, max_faults=1, extra0=ex, engine_factory=cls)),
                ("fixed-script faults, <=2 faults", lambda src, ex: skeleton_faults(topo, src, cfg, scripts(src, ex), max_faults=2, engine_factory=cls, extra0=ex)),
                ("closed-loop faults, <=2 faults", lambda src, ex: adaptive_faults(topo, src, cfg, max_faults=2, extra0=ex, engine_factory=cls)),
                ("lost-reading+reboot, <=3 faults", lambda src, ex: skeleton_faults(topo, src, cfg, scripts(src, ex), fault_events="LR", max_faults=3, engine_factory=cls, extra0=ex)),
                ("exhaustive depth 5", lambda src, ex: explore(topo, src, cfg, 5, max_faults=2, extra0=ex, engine_factory=cls)),
            ]
            budget_s, t_start, ran = float(os.environ.get("MUTANT_BUDGET_S", "420")), time.time(), []
            for stage, fn in stages:
                if time.time() - t_start > budget_s:
                    break
                ran.append(stage)
                for k, (src, ex) in TRUTHS.items():
                    st = fn(src, ex)
                    nodes += st.nodes
                    if st.violations:
                        found = dict(stage=stage, truth=k, schedule=st.violations[0][0], violation=st.violations[0][1]); break
                if found: break
            res[name] = dict(caught=bool(found), evidence=found, nodes_examined=nodes,
                             stages_run=ran, seconds=round(time.time() - t_start, 1))
            print(json.dumps({name: res[name]})[:700], flush=True)
        out("mutants", res)

main()
