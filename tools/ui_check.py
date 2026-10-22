#!/usr/bin/env python3
"""Drive the Leak Hunt console in a sandboxed iframe (dev host) with taps only, and audit it against the
Amazon CX / accessibility rules we committed to: 48x48 px targets, spoken==displayed text, contrast,
light+dark, 768x480 and 1280x800 canvases, inline mode, keyboard reachability, no overflow.

usage: python3 tools/ui_check.py [base_url [name-substring]]     (server started with tools/devserver.sh, speed >= 6)
"""
import json, os, sys, time
from urllib.parse import quote
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SHOTS = os.path.join(ROOT, "docs", "shots")
os.makedirs(SHOTS, exist_ok=True)

def chan(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

def lum(rgb): return 0.2126 * chan(rgb[0]) + 0.7152 * chan(rgb[1]) + 0.0722 * chan(rgb[2])

def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)

def rgb(s):
    import re
    m = re.findall(r"[\d.]+", s)
    return tuple(float(x) for x in m[:3])

AUDIT_JS = """() => {
  const bg = (el) => { while (el) { const c = getComputedStyle(el).backgroundColor; const m = c.match(/[\\d.]+/g);
      if (m && (m.length < 4 || parseFloat(m[3]) > 0.5)) return c; el = el.parentElement; } return 'rgb(255,255,255)'; };
  const vis = (el) => el && el.offsetParent !== null && el.getBoundingClientRect().width > 0;
  const sel = ['.say','.kicker','.know li','.hint','.btn.primary','.btn:not(.primary)','.pill.ok','.pill.warn','.pill.next','.badge.sim','.chip','.how','.valve .name','.err','h1','.btn.danger','.rules li','.tile b','.badge.auto'];
  const contrast = [];
  for (const s of sel) { const el = [...document.querySelectorAll(s)].find(vis); if (!el) continue;
      const cs = getComputedStyle(el); contrast.push({sel:s, color:cs.color, bg: s.startsWith('.pill') && s!=='.pill.next' ? bg(el.parentElement) : bg(el), fs: parseFloat(cs.fontSize), fw: parseInt(cs.fontWeight)}); }
  const btns = [...document.querySelectorAll('.btn')].filter(vis).map(b => { const r = b.getBoundingClientRect(); return {t:b.textContent, w:Math.round(r.width), h:Math.round(r.height), x:Math.round(r.x), y:Math.round(r.y)}; });
  const fixed = window.innerWidth > 560 && !document.getElementById('app').classList.contains('compact');
  const cards = [...document.querySelectorAll('.card')].filter(vis).map(c => ({id:c.id, over: fixed ? c.scrollHeight - c.clientHeight : 0}));
  const app = document.getElementById('app');
  return {contrast, btns, cards, overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          appOverflowY: app.scrollHeight - window.innerHeight, live: document.getElementById('say').getAttribute('aria-live'),
          lang: document.documentElement.lang, h1: !!document.querySelector('h1'), main: !!document.querySelector('main'),
          say: document.getElementById('say').textContent, theme: document.documentElement.getAttribute('data-theme'),
          anim: [...document.querySelectorAll('*')].filter(e => { const a = getComputedStyle(e).animationName; return a && a !== 'none'; }).length};
}"""

def audit(fr, tag, fails, big=False):
    a = fr.evaluate(AUDIT_JS)
    for b in a["btns"]:
        if b["w"] < 48 or b["h"] < 48: fails.append(f"{tag}: target {b['t']!r} is {b['w']}x{b['h']} (<48)")
    for c in a["contrast"]:
        r = ratio(rgb(c["color"]), rgb(c["bg"]))
        large = c["fs"] >= 24 or (c["fs"] >= 18.66 and c["fw"] >= 700)
        need = 3.0 if large else 4.5
        if r < need: fails.append(f"{tag}: contrast {c['sel']} {r:.2f} < {need}")
    if a["overflowX"] > 0: fails.append(f"{tag}: horizontal overflow {a['overflowX']}px")
    if a["live"] != "polite": fails.append(f"{tag}: #say not aria-live=polite")
    if a["lang"] != "en" or not a["h1"] or not a["main"]: fails.append(f"{tag}: missing lang/h1/main")
    if a["anim"]: fails.append(f"{tag}: CSS animations present ({a['anim']}) - flashing risk")
    return a

def snap(page, name):
    page.locator("#frame").screenshot(path=os.path.join(SHOTS, name + ".png"))

def pres(fr):
    return fr.evaluate("() => { const p = window.__leakhunt && window.__leakhunt.state.pres; return p ? {kind:p.kind,status:p.status,phase:p.phase,ak:p.action&&p.action.kind,speech:p.speech,found:p.found,outcome:p.outcome,url:p.url,mode:p.mode,nu:!!(p.auto&&p.auto.needs_you),kept:p.auto&&p.auto.kept_closed&&p.auto.kept_closed.map(x=>x.id),offer:p.auto&&p.auto.isolation_offer&&p.auto.isolation_offer.map(x=>x.id),kicker:(document.querySelector('.kicker')||{}).textContent,say:document.getElementById('say').textContent} : null }")

def wait_ready(page, fr, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        try:
            if page.evaluate("() => window.__host && window.__host.ready") and pres(fr): return
        except Exception: pass
        time.sleep(0.2)
    raise RuntimeError("host not ready")

def click_primary(fr):
    b = fr.locator(".actions .btn.primary").first
    b.click(timeout=5000)

def verdict_problems(page):
    """The verdict card is what a viewer must see at the moment of Reveal, with no scrolling, next to the screen."""
    g = page.evaluate("""() => { const r = e => e.getBoundingClientRect(); const v = r(document.getElementById('verdict')), f = r(document.getElementById('frame')), a = r(document.getElementById('ch'));
      return {vt: v.top, vb: v.bottom, vl: v.left, vr: v.right, fr: f.right, al: a.left, ab: a.bottom, iw: window.innerWidth, ih: window.innerHeight, sy: window.scrollY,
              sh: document.documentElement.scrollHeight, sw: document.documentElement.scrollWidth}; }""")
    out = []
    if g["vt"] < 0 or g["vb"] > g["ih"] + 1: out.append(f"the verdict is not fully visible without scrolling (top {g['vt']:.0f}, bottom {g['vb']:.0f}, window {g['ih']})")
    if g["ab"] > g["ih"] + 1: out.append(f"the side column runs past the window ({g['ab']:.0f} > {g['ih']})")
    if g["al"] < g["fr"]: out.append("the side column is not beside the screen")
    if g["sy"] != 0 or g["sh"] > g["ih"] + 2: out.append(f"the page needs scrolling (height {g['sh']}, window {g['ih']})")
    if g["vr"] > g["iw"] + 1 or g["sw"] > g["iw"] + 1: out.append("the verdict spills sideways")
    return out

def run_scenario(pw, name, theme, w, h, mode="fullscreen", first="water_status", args=None, shots=("start", "done"), keyboard=False, ev=False, flow="guided", reopen=False, challenge=False, answer="keep"):
    fails, res = [], {"name": name, "theme": theme, "size": f"{w}x{h}", "mode": mode}
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": max(w + 40, 800) + (600 if challenge else 0), "height": h + 140}, color_scheme=theme)
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    q = f"?theme={theme}&w={w}&h={h}&mode={mode}&tool={first}" + (f"&args={quote(json.dumps(args))}" if args else "") + ("&challenge=1" if challenge else "")
    page.goto(BASE + "/devhost" + q)
    fr = page.frame_locator("#frame")
    frame = next(f for f in page.frames if f != page.main_frame) if False else None
    # Playwright Frame (for evaluate): wait for the iframe to exist
    for _ in range(100):
        fl = [f for f in page.frames if f != page.main_frame]
        if fl: frame = fl[0]; break
        time.sleep(0.1)
    wait_ready(page, frame)
    a = audit(frame, f"{name}/first", fails)
    if "start" in shots: snap(page, f"{name}_01_first")
    if challenge:
        page.locator("#scripted").click(timeout=5000)
        page.wait_for_function("() => document.getElementById('round').textContent.indexOf('scripted') >= 0", timeout=8000)
        page.screenshot(path=os.path.join(SHOTS, f"{name}_05_round_hidden.png"))
    if first == "water_status":
        p0 = pres(frame)
        if p0["speech"] != a["say"]: fails.append(f"{name}: status screen text != speech")
        snap(page, f"{name}_00_status")
        if keyboard:
            # keyboard-only: tab order reaches the actions and Enter activates
            frame.evaluate("() => document.body.focus()")
            seen = []
            for _ in range(6):
                page.keyboard.press("Tab")
                seen.append(frame.evaluate("() => { const e = document.activeElement; if (!e) return null; const cs = getComputedStyle(e); return {tag:e.tagName, id:e.id, t:(e.textContent||'').slice(0,24), ow: cs.outlineWidth, os: cs.outlineStyle}; }"))
            btns_focus = [s for s in seen if s and s["tag"] == "BUTTON"]
            if not btns_focus: fails.append(f"{name}: no button reachable by Tab")
            for s in btns_focus:
                if s["os"] == "none" or float(s["ow"].replace("px", "")) < 2: fails.append(f"{name}: focus ring missing on {s['t']!r}")
            res["tab_order"] = seen
        click_primary(frame)
        time.sleep(0.8)
    steps, polls0 = 0, None
    t0 = time.time(); last = None; parity = 0; seen_auto = False; seen_consent = False
    auto = flow == "auto"
    while time.time() - t0 < 150:
        p = pres(frame)
        if not p: time.sleep(0.3); continue
        a = p["say"]                      # read in the same JS turn as the state, so a poll cannot slip in between
        if p["kind"] in ("hunt", "choose_mode", "needs_consent", "pipes"):
            if a != p["speech"]: fails.append(f"{name}: say!=speech for {p['kind']} at step {steps}: {a!r} vs {p['speech']!r}")
            else: parity += 1
        if p["kind"] == "choose_mode":
            audit(frame, f"{name}/choose", fails)
            if "start" in shots: snap(page, f"{name}_02_choose")
            frame.get_by_role("button", name="Run it for me" if auto else "Guide me", exact=True).click(timeout=5000)
            time.sleep(0.7); continue
        if p["kind"] == "needs_consent":
            if not auto: fails.append(f"{name}: consent asked in a guided scenario")
            if not seen_consent:
                seen_consent = True; audit(frame, f"{name}/consent", fails); snap(page, f"{name}_03_consent")
            frame.get_by_role("button", name="Yes, run the test" if auto else "No, guide me", exact=True).click(timeout=5000)
            time.sleep(0.7); continue
        if p["kind"] == "hunt" and p["status"] != "active": break
        if p["kind"] == "hunt" and p["mode"] == "auto":
            if not seen_auto and p["ak"] in ("wait", "close", "open", "set_state") and not p["nu"]:
                seen_auto = True; audit(frame, f"{name}/auto-run", fails); snap(page, f"{name}_04_autorun")
                if not frame.get_by_role("button", name="Stop the test", exact=True).is_visible(): fails.append(f"{name}: no visible Stop button while Alexa runs the test")
        needs = (p["ak"] in ("quiet", "close", "open", "set_state")) and (p["nu"] if auto else True)
        if needs:
            key = (p["ak"], p["speech"])
            if key != last:
                steps += 1; last = key
                if steps in (1, 2, 3) and ("steps" in shots):
                    audit(frame, f"{name}/step{steps}", fails); snap(page, f"{name}_step{steps}")
            try: click_primary(frame)
            except Exception as e: fails.append(f"{name}: click failed {e}")
            time.sleep(0.6)
        else:
            if "wait" in shots and p["ak"] == "wait" and not os.path.exists(os.path.join(SHOTS, f"{name}_wait.png")):
                snap(page, f"{name}_wait")
            time.sleep(0.5)
    p = pres(frame)
    if challenge:
        if not (p and p["status"] == "done" and p["outcome"] == "only_sources" and len(p["found"] or []) == 1):
            fails.append(f"{name}: scripted hunt did not finish with one isolated source: {p}")
    elif not (p and p["status"] == "done" and p["outcome"] == "only_sources" and p["found"] == ["toilet"]):
        fails.append(f"{name}: hunt did not finish with toilet/only_sources: {p}")
    ad = audit(frame, f"{name}/done", fails)
    for c in ad["cards"]:
        if c["over"] > 2: fails.append(f"{name}: card {c['id']} overflows by {c['over']}px at result (scrolls)")
    snap(page, f"{name}_90_done")
    if auto and challenge:
        if frame.evaluate("() => window.__leakhunt.state.pres.auto.running"): fails.append(f"{name}: auto still running at done")
        time.sleep(2.5)                      # the panel re-reads the round every 2 s; the script switched the tap off during the hunt
        flags_txt = page.locator("#flags").inner_text()
        if "outside tap running" in flags_txt: fails.append(f"{name}: the panel still says the outside tap is running after the script switched it off: {flags_txt!r}")
        page.locator("#reveal").click(timeout=5000)
        page.wait_for_function("() => document.querySelectorAll('#verdict .vrow').length >= 3", timeout=8000)
        v_txt = page.locator("#verdict").inner_text()
        res["reveal"] = v_txt
        leak_line = page.locator("#verdict .vrow", has_text="Leak Hunt").inner_text()
        naive_line = page.locator("#verdict .vrow", has_text="Simplest rule").inner_text()
        if "garden tap" not in leak_line: fails.append(f"{name}: Reveal does not show the right valve: {v_txt!r}")
        if "WRONG" not in naive_line: fails.append(f"{name}: Reveal does not show the simplest rule being wrong: {v_txt!r}")
        if "chosen" not in v_txt: fails.append(f"{name}: a scripted round does not say its timing was chosen: {v_txt!r}")
        if "not a commercial product" not in v_txt: fails.append(f"{name}: the comparison is not labelled as one: {v_txt!r}")
        if page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1"): fails.append(f"{name}: challenge page scrolls sideways")
        probs = verdict_problems(page)
        fails += [f"{name}: {x}" for x in probs]
        page.screenshot(path=os.path.join(SHOTS, f"{name}_06_reveal.png"))          # the window as a viewer sees it: no scrolling
        # the check must be able to fail: push the side column below the screen and it has to notice
        broken = page.add_style_tag(content="#stage{display:block !important}")
        res["broken_variant_verdict_below_the_fold_caught"] = bool(verdict_problems(page))
        broken.evaluate("e => e.remove()")
        if not res["broken_variant_verdict_below_the_fold_caught"]: fails.append(f"{name}: the above-the-fold check did not notice a verdict pushed below the fold")
    elif auto:
        # a found leak is only HELD under a lease with a question; it is not kept closed until the customer says yes
        if p.get("kept"): fails.append(f"{name}: toilet already latched before the customer said yes: {p.get('kept')}")
        if p.get("offer") != ["toilet"]: fails.append(f"{name}: expected an open 'keep it closed?' question for the toilet, got {p.get('offer')}")
        if "keep it off" not in (p.get("speech") or "") or "turn it back on" not in (p.get("speech") or ""): fails.append(f"{name}: the question is not in the spoken text: {p.get('speech')!r}")
        if p.get("kicker") != "Keep it closed?": fails.append(f"{name}: screen kicker {p.get('kicker')!r}")
        if frame.evaluate("() => window.__leakhunt.state.pres.auto.running"): fails.append(f"{name}: auto still running at done")
        phys = page.evaluate("() => window.__host.results.filter(r => r && r.kind === 'hunt').slice(-1)[0].nodes.filter(n => n.state === 'closed').map(n => n.id)")
        if phys != ["toilet"]: fails.append(f"{name}: closed pipes while the question is open {phys}")
        label = "Keep it closed" if answer == "keep" else "Turn it back on"
        time.sleep(1.0)        # the service never flips one pipe twice within 2 s; at x6 time-lapse that is a third of a second
        frame.get_by_role("button", name=label, exact=True).first.click(timeout=5000); time.sleep(1.2)
        p = pres(frame)
        if p.get("offer"): fails.append(f"{name}: question still open after answering {answer!r}: {p.get('offer')}")
        if p.get("kind") != "pipes": fails.append(f"{name}: no pipes screen after {answer!r}: {p.get('kind')}")
        rows = frame.evaluate("() => (window.__leakhunt.state.pres.pipes || []).map(r => ({id: r.id, pos: r.position, latched: !!r.latched}))")
        want = ["toilet"] if answer == "keep" else []
        if sorted(r["id"] for r in rows if r["pos"] == "closed") != want: fails.append(f"{name}: closed pipes after {answer!r}: {rows}")
        if sorted(r["id"] for r in rows if r["latched"]) != want: fails.append(f"{name}: kept-closed pipes after {answer!r}: {rows}")
        audit(frame, f"{name}/answered", fails); snap(page, f"{name}_93_answered")
        if answer == "keep" and reopen:
            # turning a kept-closed pipe back on asks first, because it was closed on purpose
            frame.get_by_role("button", name="Turn on", exact=True).first.click(timeout=5000); time.sleep(1.0)
            pc = pres(frame)
            if not pc or pc["kind"] != "needs_confirm": fails.append(f"{name}: reopening a leaking pipe did not ask first: {pc}")
            else:
                audit(frame, f"{name}/confirm", fails); snap(page, f"{name}_91_confirm")
                frame.get_by_role("button", name="Yes, turn it on", exact=True).click(timeout=5000); time.sleep(1.0)
                pp = pres(frame)
                if not pp or pp["kind"] != "pipes": fails.append(f"{name}: no pipes screen after confirming: {pp}")
                else:
                    audit(frame, f"{name}/pipes", fails); snap(page, f"{name}_92_pipes")
                    off = frame.evaluate("() => window.__leakhunt.state.pres.pipes.filter(r => r.position === 'closed' || r.latched).map(r => r.id)")
                    if off: fails.append(f"{name}: pipes still off or kept after reopening: {off}")
    if ev and p and p["status"] == "done":
        click_primary(frame)
        time.sleep(1.0)
        pe = pres(frame)
        if not pe or pe["kind"] != "evidence": fails.append(f"{name}: evidence screen not shown: {pe}")
        else:
            audit(frame, f"{name}/evidence", fails); snap(page, f"{name}_95_evidence")
            frame.locator(".actions .btn.primary").first.click(); time.sleep(0.5)
            links = page.evaluate("() => window.__host.links")
            if not links: fails.append(f"{name}: evidence link was not handed to the host")
            res["links"] = links
    calls = page.evaluate("() => window.__host.calls")
    app_calls = [c["name"] for c in calls if c["by"] == "app"]
    res.update(steps=steps, parity_checks=parity, polls=app_calls.count("hunt_status"), taps=len(app_calls) - app_calls.count("hunt_status"),
               seconds=round(time.time() - t0, 1), js_errors=errs[:3])
    if errs: fails.append(f"{name}: console/page errors: {errs[:2]}")
    if app_calls.count("hunt_status") < 2: fails.append(f"{name}: UI did not poll for results")
    res["failures"] = fails
    ctx.close(); br.close()
    return res

def plug_problems(page, expect_down, expect_open_again=False):
    """What the screen must show while the add-on is dead: a veil over the app, the controller panel, no calls getting through."""
    g = page.evaluate("""() => { const v = document.getElementById('veil'), vr = v.getBoundingClientRect(), fr = document.getElementById('frame').getBoundingClientRect(),
        cs = getComputedStyle(v);
      return {down: window.__host.addonDown, veil: !v.hidden && cs.display !== 'none', covers: Math.abs(vr.width - fr.width) < 2 && Math.abs(vr.height - fr.height) < 2,
              text: v.innerText, plugHidden: document.getElementById('plug').hidden, backShown: !document.getElementById('plugback').hidden,
              note: document.getElementById('plugnote').innerText, closed: document.querySelectorAll('#pipes li.closed').length,
              panelTag: document.querySelector('#ctl .tag').innerText, panelTitle: document.querySelector('#ctl h3').innerText}; }""")
    out = []
    if "simulated" not in g["panelTag"].lower() or "controller" not in g["panelTitle"].lower(): out.append("the controller panel does not say it is a simulated controller")
    if expect_down:
        if not g["down"] or not g["veil"]: out.append("the add-on is dead but the screen is not veiled")
        if not g["covers"]: out.append("the veil does not cover the add-on's screen")
        if "No answer from the add-on" not in g["text"]: out.append(f"the veil does not say what happened: {g['text']!r}")
        if g["plugHidden"] is not True or not g["backShown"]: out.append("the plug/restart buttons are not swapped while dead")
        if expect_open_again and ("The controller's own timer:" not in g["note"] or g["closed"]): out.append(f"the panel does not show the controller reopening the pipes: {g['note']!r}, closed={g['closed']}")
    else:
        if g["veil"] or g["down"]: out.append("the screen is still veiled")
    return out

def run_plug(pw, name, theme, w, h):
    """Pull the plug on the dev host: kill the add-on while a pipe is closed and watch the simulated controller reopen it."""
    fails, res = [], {"name": name, "theme": theme, "size": f"{w}x{h}", "mode": "fullscreen", "steps": 0, "parity_checks": 0, "polls": 0, "taps": 0, "seconds": 0}
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": max(w + 40, 800) + 600, "height": h + 140}, color_scheme=theme)
    page = ctx.new_page(); errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.goto(BASE + f"/devhost?theme={theme}&w={w}&h={h}&mode=fullscreen&tool=start_leak_hunt&args={quote(json.dumps({'resume': False}))}&challenge=1")
    frame = None
    for _ in range(100):
        fl = [f for f in page.frames if f != page.main_frame]
        if fl: frame = fl[0]; break
        time.sleep(0.1)
    wait_ready(page, frame)
    page.locator("#seed").fill("7")                                # a fixed hidden round in which the hunt closes two pipes; a random
    page.locator("#new").click(timeout=5000)                       #   round can hide no leak, and then nothing stays closed to pull on
    page.wait_for_function("() => document.getElementById('round').textContent.indexOf('Round') >= 0", timeout=8000)
    t0 = time.time(); started = False
    while time.time() - t0 < 60:                                   # answer the two questions, then wait for pipes to be closed
        p = pres(frame)
        if p and p["kind"] == "choose_mode": frame.get_by_role("button", name="Run it for me", exact=True).click(timeout=5000); time.sleep(0.7); continue
        if p and p["kind"] == "needs_consent": frame.get_by_role("button", name="Yes, run the test", exact=True).click(timeout=5000); time.sleep(0.7); continue
        if page.locator("#pipes li.closed").count() >= 2: started = True; break
        if p and p["kind"] == "hunt" and p["status"] == "active" and p.get("nu"):      # Alexa needs the customer's word that the house is quiet
            try: click_primary(frame)
            except Exception: pass
            time.sleep(0.6); continue
        time.sleep(0.4)
    if not started: fails.append(f"{name}: the hunt never closed two pipes"); res["failures"] = fails; ctx.close(); br.close(); return res
    fails += [f"{name}/before: {x}" for x in plug_problems(page, expect_down=False)]
    snap_full = os.path.join(SHOTS, f"{name}_01_before.png"); page.screenshot(path=snap_full)
    calls_before = page.evaluate("() => window.__host.calls.filter(c => c.by === 'app').length")
    page.locator("#plug").click(timeout=5000)
    page.wait_for_function("() => window.__host.addonDown === true", timeout=5000)
    time.sleep(3.0)                                                # the screen keeps trying to poll; none of it may get through
    calls_after = page.evaluate("() => window.__host.calls.filter(c => c.by === 'app').length")
    refused = page.evaluate("() => window.__host.refused")
    if calls_after != calls_before: fails.append(f"{name}: {calls_after - calls_before} app calls reached the add-on while it was dead")
    if refused < 1: fails.append(f"{name}: the screen never tried to reach the dead add-on, so the veil was not exercised")
    fails += [f"{name}/dead: {x}" for x in plug_problems(page, expect_down=True)]
    page.screenshot(path=os.path.join(SHOTS, f"{name}_02_plug_pulled.png"))
    t1 = time.time(); reopened = False
    while time.time() - t1 < 90:
        if page.locator("#pipes li.closed").count() == 0 and "back to its safe state" in page.locator("#plugnote").inner_text(): reopened = True; break
        time.sleep(0.5)
    res["wall_seconds_to_reopen"] = round(time.time() - t1, 1)
    if not reopened: fails.append(f"{name}: the pipes did not reopen on the panel within 90 s")
    ctl = page.evaluate("async () => (await fetch('/challenge/hub')).json()")
    res["controller"] = dict(silent_s=ctl["silent_s"], lease_s=ctl["lease_s"], lapsed=ctl["lapsed_since_plug_pulled"])
    if not ctl["lapsed_since_plug_pulled"]: fails.append(f"{name}: the controller reports no lapsed lease")
    for l in ctl["lapsed_since_plug_pulled"]:
        if l["after_s"] > ctl["lease_s"] + 1: fails.append(f"{name}: {l['label']} stayed closed {l['after_s']} s, more than the {ctl['lease_s']} s lease")
    fails += [f"{name}/reopened: {x}" for x in plug_problems(page, expect_down=True, expect_open_again=True)]
    page.screenshot(path=os.path.join(SHOTS, f"{name}_03_reopened.png"))
    # the check must be able to fail: take the veil away and it has to notice
    page.evaluate("() => document.getElementById('veil').hidden = true")
    res["broken_variant_missing_veil_caught"] = bool(plug_problems(page, expect_down=True))
    if not res["broken_variant_missing_veil_caught"]: fails.append(f"{name}: the check did not notice a missing veil")
    page.locator("#plugback").click(timeout=5000)
    page.wait_for_function("() => window.__host.addonDown === false", timeout=5000)
    fails += [f"{name}/back: {x}" for x in plug_problems(page, expect_down=False)]
    time.sleep(4.0)
    p = pres(frame)
    res["after_restart"] = dict(status=p and p["status"], outcome=p and p["outcome"], speech=(p or {}).get("speech"))
    if not p or p["status"] == "active": fails.append(f"{name}: the hunt is still running after the add-on came back to pipes it did not leave: {p}")
    elif "rather than guess" not in (p.get("speech") or ""): fails.append(f"{name}: the add-on did not stop rather than guess after restart: {p.get('speech')!r}")
    res["failures"] = fails + ([f"{name}: console/page errors: {errs[:2]}"] if errs else [])
    res.update(steps=0, parity_checks=0, polls=0, taps=0, seconds=round(time.time() - t0, 1), js_errors=errs[:3])
    ctx.close(); br.close()
    return res

def fill_problems(frame, active):
    """What the fill screen must show: the amount, and while filling the controller's own timer and a Stop button."""
    g = frame.evaluate("""() => { const t = document.querySelector('.timer'), b = document.querySelector('.gauge .big'), bar = document.querySelector('.bar i');
      const stop = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Stop filling');
      const vis = e => !!e && e.offsetParent !== null && e.getBoundingClientRect().height > 0;
      return {timer: vis(t) ? t.innerText : '', big: vis(b) ? b.innerText : '', bar: !!bar, stop: vis(stop)}; }""")
    out = []
    if "litres" not in g["big"]: out.append("the amount filled is not shown")
    if not g["bar"]: out.append("no progress bar")
    if active:
        if "closes it by itself" not in g["timer"]: out.append(f"the controller's own timer is not shown while filling: {g['timer']!r}")
        if not g["stop"]: out.append("no visible Stop button while filling")
    return out

def wire_problems(page):
    """The dev host must be a real MCP client over Streamable HTTP (initialize, tools/list, resources/read, tools/call), and say so
    on screen."""
    w = page.evaluate("""() => { const e = document.getElementById('wire'); const H = window.__host;
      return {text: e && e.offsetParent !== null && e.getBoundingClientRect().width > 0 ? e.innerText : '', wire: H.wire, tools: H.tools.length}; }""")
    out = []
    for m in ("initialize", "tools/list", "resources/read", "tools/call"):
        if not w["wire"].get(m): out.append(f"the page never sent {m}")
        if m not in w["text"]: out.append(f"{m} is not shown in the top bar")
    if w["tools"] != 14: out.append(f"tools/list returned {w['tools']} tools, expected 14")
    return out

def run_fill_ui(pw, name, theme, w, h, outlet="bath", litres=40, start_text="Filling the bathtub with 40 litres", end_text="litres went in"):
    """Fill from the screen: the amount, the controller's timer, Stop, the count at the end, and the offer to find where other water
    is going (a leak hunt), all with taps only."""
    fails, res = [], {"name": name, "theme": theme, "size": f"{w}x{h}", "mode": "fullscreen", "steps": 0, "parity_checks": 0, "polls": 0, "taps": 0, "seconds": 0}
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": max(w + 40, 800), "height": h + 140}, color_scheme=theme)
    page = ctx.new_page(); errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.goto(BASE + f"/devhost?theme={theme}&w={w}&h={h}&mode=fullscreen&tool=fill_water&args={quote(json.dumps({'outlet': outlet, 'litres': litres}))}")
    frame = None
    for _ in range(100):
        fl = [f for f in page.frames if f != page.main_frame]
        if fl: frame = fl[0]; break
        time.sleep(0.1)
    wait_ready(page, frame)
    t0 = time.time()
    p = frame.evaluate("() => window.__leakhunt.state.pres")
    if not p or p.get("kind") != "fill": fails.append(f"{name}: first screen is not the fill screen: {p and p.get('kind')}"); res["failures"] = fails; ctx.close(); br.close(); return res
    if start_text not in p["speech"]: fails.append(f"{name}: start speech {p['speech']!r}")
    time.sleep(2.0)
    fails += [f"{name}/wire: {x}" for x in wire_problems(page)]
    hidden = page.add_style_tag(content="#wire{display:none !important}")       # the check must be able to fail
    res["broken_variant_hidden_wire_caught"] = bool(wire_problems(page))
    hidden.evaluate("e => e.remove()")
    if not res["broken_variant_hidden_wire_caught"]: fails.append(f"{name}: the check did not notice a hidden MCP request strip")
    audit(frame, f"{name}/filling", fails)
    fails += [f"{name}/filling: {x}" for x in fill_problems(frame, True)]
    snap(page, f"{name}_01_filling")
    # the check must be able to fail: hide the controller's timer while filling and it has to notice
    hidden = frame.add_style_tag(content=".timer{display:none !important}")
    res["broken_variant_hidden_timer_caught"] = bool(fill_problems(frame, True))
    hidden.evaluate("e => e.remove()")
    if not res["broken_variant_hidden_timer_caught"]: fails.append(f"{name}: the check did not notice a hidden controller timer")
    seen, parity, last_d = set(), 0, -1
    while time.time() - t0 < 120:
        q = frame.evaluate("() => { const p = window.__leakhunt.state.pres; const s = document.getElementById('say'); return {p: p, say: s && s.textContent}; }")
        p = q["p"]
        if p["speech"] != q["say"]: fails.append(f"{name}: say != speech: {q['say']!r} vs {p['speech']!r}")
        else: parity += 1
        if p["delivered_l"] > last_d: seen.add(round(p["delivered_l"])); last_d = p["delivered_l"]
        if p["status"] != "active": break
        time.sleep(0.5)
    res.update(parity_checks=parity, progress_values=sorted(seen)[:12], seconds=round(time.time() - t0, 1))
    if len(seen) < 3: fails.append(f"{name}: the amount did not visibly grow while filling: {sorted(seen)}")
    if p["status"] != "done" or p["outcome"] != "done": fails.append(f"{name}: fill did not finish: {p['status']} {p['outcome']}")
    offer = bool(p.get("offer_hunt"))                 # the offer depends on whether other water was moving, which earlier scenarios change
    if end_text not in p["speech"] or (("find where it's going" in p["speech"]) != offer): fails.append(f"{name}: end speech {p['speech']!r} (offer {offer})")
    if abs(p["delivered_l"] - litres) > 1.5: fails.append(f"{name}: said {p['delivered_l']} litres for {litres}")
    time.sleep(0.8)
    audit(frame, f"{name}/done", fails)
    fails += [f"{name}/done: {x}" for x in fill_problems(frame, False)]
    snap(page, f"{name}_02_done")
    btn = frame.get_by_role("button", name="Find where it’s going", exact=True)
    res["offer_hunt"] = offer
    if not offer:
        if btn.count() and btn.is_visible(): fails.append(f"{name}: offers to find other water when none was moving")
    elif not btn.is_visible(): fails.append(f"{name}: no button to find where the other water goes")
    else:
        btn.click(timeout=5000)
        time.sleep(1.5)
        k = frame.evaluate("() => window.__leakhunt.state.pres && window.__leakhunt.state.pres.kind")
        if k != "choose_mode": fails.append(f"{name}: 'Find where it's going' did not start a leak hunt: {k}")
        else: snap(page, f"{name}_03_find")
    res["failures"] = fails + ([f"{name}: console/page errors: {errs[:2]}"] if errs else [])
    res["js_errors"] = errs[:3]
    ctx.close(); br.close()
    return res

def run_pipes(pw, name, theme, w, h):
    """Voice-style pipe control on screen: the pipes screen, a toggle, an ambiguous name resolved by tapping, and 'both'."""
    fails, res = [], {"name": name, "theme": theme, "size": f"{w}x{h}", "mode": "fullscreen"}
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": max(w + 40, 800), "height": h + 140}, color_scheme=theme)
    page = ctx.new_page(); errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.goto(BASE + f"/devhost?theme={theme}&w={w}&h={h}&tool=list_pipes")
    frame = None
    for _ in range(100):
        fl = [f for f in page.frames if f != page.main_frame]
        if fl: frame = fl[0]; break
        time.sleep(0.1)
    wait_ready(page, frame)
    p0 = pres(frame)
    if p0["kind"] != "pipes": fails.append(f"{name}: first screen is {p0['kind']}")
    audit(frame, f"{name}/pipes", fails); snap(page, f"{name}_01_pipes")
    def pipes(): return frame.evaluate("() => Object.fromEntries(window.__leakhunt.state.pres.pipes.map(r => [r.id, r.position]))")
    def tile_btn(label): return frame.locator(".tile", has_text=label).locator("button")
    if any(v != "open" for v in pipes().values()): fails.append(f"{name}: not every pipe starts on: {pipes()}")
    tile_btn("kitchen pipe 2").click(timeout=5000); time.sleep(0.8)
    st = pipes()
    if st.get("kitchen2") != "closed" or st.get("kitchen1") != "open": fails.append(f"{name}: toggling kitchen pipe 2 gave {st}")
    pp = pres(frame)
    if pp["say"] != "Kitchen pipe 2 is off.": fails.append(f"{name}: spoken text after toggle: {pp['say']!r}")
    audit(frame, f"{name}/after-toggle", fails); snap(page, f"{name}_02_toggled")
    time.sleep(2.5)                                   # the service never flips one pipe twice within 2 s
    tile_btn("kitchen pipe 2").click(timeout=5000); time.sleep(0.8)
    if pipes().get("kitchen2") != "open": fails.append(f"{name}: kitchen pipe 2 did not come back on")
    page.close(); ctx.close()
    ctx = br.new_context(viewport={"width": max(w + 40, 800), "height": h + 140}, color_scheme=theme)
    page = ctx.new_page()
    page.on("pageerror", lambda e: errs.append(str(e)))
    time.sleep(2.5)
    page.goto(BASE + f"/devhost?theme={theme}&w={w}&h={h}&tool=set_pipe&args=" + quote(json.dumps({"pipe": "kitchen", "state": "off"})))
    frame = None
    for _ in range(100):
        fl = [f for f in page.frames if f != page.main_frame]
        if fl: frame = fl[0]; break
        time.sleep(0.1)
    wait_ready(page, frame)
    pc = pres(frame)
    if pc["kind"] != "choose_pipe": fails.append(f"{name}: 'kitchen' did not ask which pipe: {pc['kind']}")
    elif pc["say"] != pc["speech"]: fails.append(f"{name}: choose_pipe say != speech")
    audit(frame, f"{name}/choose-pipe", fails); snap(page, f"{name}_03_which")
    names = frame.evaluate("() => [...document.querySelectorAll('.actions .btn')].map(b => b.textContent)")
    if "kitchen pipe 1" not in names or "kitchen pipe 2" not in names or "Both" not in names: fails.append(f"{name}: options {names}")
    frame.get_by_role("button", name="Both", exact=True).click(timeout=5000); time.sleep(1.0)
    st = pipes()
    if st.get("kitchen1") != "closed" or st.get("kitchen2") != "closed" or st.get("toilet") != "open": fails.append(f"{name}: 'both kitchen pipes' gave {st}")
    audit(frame, f"{name}/both-off", fails); snap(page, f"{name}_04_both_off")
    errs = [e for e in errs if "Failed to load resource" not in e]
    if errs: fails.append(f"{name}: console/page errors: {errs[:2]}")
    res.update(steps=0, parity_checks=1, polls=0, taps=0, seconds=0, js_errors=errs[:3], failures=fails)
    ctx.close(); br.close()
    return res

def mode_problems(frame):
    """What the house-mode screen must show: the question before anything moves, the meter check once it is on, and a way back."""
    g = frame.evaluate("""() => { const vis = e => !!e && e.offsetParent !== null && e.getBoundingClientRect().height > 0;
      const btn = t => [...document.querySelectorAll('.actions .btn')].find(b => b.textContent.trim().startsWith(t));
      const p = window.__leakhunt.state.pres || {};
      return {status: p.status, yes: vis(btn('Yes, turn')), back: p.back_label ? vis(btn(p.back_label)) : false,
              tiles: document.querySelectorAll('.tile').length, say: (document.getElementById('say') || {}).textContent || '', speech: p.speech || ''}; }""")
    out = []
    if g["say"] != g["speech"]: out.append("spoken text differs from the screen")
    if g["tiles"] < 6: out.append(f"the pipes are not shown ({g['tiles']} tiles)")
    if g["status"] == "needs_confirm" and not g["yes"]: out.append("no visible button to say yes")
    if g["status"] in ("on", "verifying") and not g["back"]: out.append("no visible button to end the mode")
    return out

def run_modes_ui(pw, name, theme, w, h):
    """Sleep mode from the screen: the question first, nothing moves before yes, the meter check, then 'I'm up' restores."""
    fails, res = [], {"name": name, "theme": theme, "size": f"{w}x{h}", "mode": "fullscreen", "steps": 0, "parity_checks": 0, "polls": 0, "taps": 0, "seconds": 0}
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": max(w + 40, 800), "height": h + 140}, color_scheme=theme)
    page = ctx.new_page(); errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.goto(BASE + f"/devhost?theme={theme}&w={w}&h={h}&mode=fullscreen&tool=house_mode&args={quote(json.dumps({'mode': 'sleep'}))}")
    frame = None
    for _ in range(100):
        fl = [f for f in page.frames if f != page.main_frame]
        if fl: frame = fl[0]; break
        time.sleep(0.1)
    wait_ready(page, frame)
    t0 = time.time()
    def pipes(): return frame.evaluate("() => Object.fromEntries((window.__leakhunt.state.pres.pipes || []).map(r => [r.id, r.position]))")
    p = frame.evaluate("() => window.__leakhunt.state.pres")
    if not p or p.get("kind") != "mode" or p.get("status") != "needs_confirm":
        fails.append(f"{name}: first screen is not the sleep-mode question: {p and (p.get('kind'), p.get('status'))}")
        res["failures"] = fails; ctx.close(); br.close(); return res
    before = pipes()                                   # an earlier scenario may have left pipes off: the mode must leave those alone
    plan = list(p.get("to_close") or [])
    time.sleep(1.0)
    if pipes() != before: fails.append(f"{name}: pipes moved before the yes: {before} -> {pipes()}")
    audit(frame, f"{name}/ask", fails); fails += [f"{name}/ask: {x}" for x in mode_problems(frame)]; snap(page, f"{name}_01_ask")
    hidden = frame.add_style_tag(content=".actions .btn.primary{display:none !important}")      # the check must be able to fail
    res["broken_variant_hidden_yes_caught"] = bool(mode_problems(frame))
    hidden.evaluate("e => e.remove()")
    if not res["broken_variant_hidden_yes_caught"]: fails.append(f"{name}: the check did not notice a hidden yes button")
    frame.locator(".actions .btn.primary", has_text="Yes, turn").first.click(timeout=5000)
    st = None
    while time.time() - t0 < 60:
        st = frame.evaluate("() => window.__leakhunt.state.pres")
        if st and st.get("status") == "on": break
        time.sleep(0.5)
    if not st or st.get("status") != "on": fails.append(f"{name}: the meter check never came in: {st and st.get('status')}")
    else:
        now = pipes()
        if not plan or any(now.get(i) != "closed" for i in plan) or now.get("bath") != "open" or "bath" in plan:
            fails.append(f"{name}: sleep mode planned {plan} and left {now}")
        if "meter" not in st.get("speech", ""): fails.append(f"{name}: no meter check in {st.get('speech')!r}")
    time.sleep(0.6)
    audit(frame, f"{name}/on", fails); fails += [f"{name}/on: {x}" for x in mode_problems(frame)]; snap(page, f"{name}_02_on")
    frame.get_by_role("button", name="I'm up", exact=True).click(timeout=5000)
    time.sleep(1.0)
    after = pipes()
    if after != before: fails.append(f"{name}: 'I'm up' left {after}, not what was there before the mode: {before}")
    audit(frame, f"{name}/home", fails); snap(page, f"{name}_03_home")
    res.update(seconds=round(time.time() - t0, 1), parity_checks=3, taps=2)
    errs = [e for e in errs if "Failed to load resource" not in e]
    res["failures"] = fails + ([f"{name}: console/page errors: {errs[:2]}"] if errs else [])
    res["js_errors"] = errs[:3]
    ctx.close(); br.close()
    return res

WATER_RX = r"\b(litres?|L/min|water|pipes?|valves?|leaks?|taps?|cisterns?|plumb\w*|flow\w*|tub|bath\w*)\b"

def power_problems(frame):
    """Power Hunt reuses the screen. What it must show: power words only (visible text and aria-labels), the same words spoken and
    shown, and never a house-mode button (modes are for pipes)."""
    g = frame.evaluate("""(rx) => { const re = new RegExp(rx, 'i'); const app = document.getElementById('app');
      const p = window.__leakhunt.state.pres || {};
      const labels = [...app.querySelectorAll('[aria-label]')].map(e => e.getAttribute('aria-label')).filter(t => re.test(t));
      const lines = app.innerText.split('\\n').filter(t => re.test(t));
      const modes = [...document.querySelectorAll('.actions .btn')].filter(b => /mode/i.test(b.textContent)).length;
      return {lines, labels, modes, h1: document.querySelector('h1').textContent, title: document.title, utility: p.utility || '',
              say: (document.getElementById('say') || {}).textContent || '', speech: p.speech || ''}; }""", WATER_RX)
    out = []
    if g["utility"] != "power": out.append(f"the screen is not showing a power presentation (utility {g['utility']!r})")
    if g["lines"] or g["labels"]: out.append(f"water wording on the power screen: {(g['lines'] + g['labels'])[:3]}")
    if g["h1"] != "Power Hunt" or g["title"] != "Power Hunt": out.append(f"heading/title say {g['h1']!r}/{g['title']!r}")
    if g["say"] != g["speech"]: out.append("spoken text differs from the screen")
    if g["modes"]: out.append("a house-mode button is offered for plugs")
    return out

def run_power_ui(pw, name, theme, w, h):
    """Power Hunt from the screen: what runs all night, Alexa's offer, consent, the automatic hunt, the result, and 'turn it back on',
    with taps only, in power words throughout."""
    fails, res = [], {"name": name, "theme": theme, "size": f"{w}x{h}", "mode": "fullscreen", "steps": 0, "parity_checks": 0, "polls": 0, "taps": 0, "seconds": 0}
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": max(w + 40, 800), "height": h + 140}, color_scheme=theme)
    page = ctx.new_page(); errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.goto(BASE + f"/devhost?theme={theme}&w={w}&h={h}&mode=fullscreen&tool=water_status&args={quote(json.dumps({'utility': 'power'}))}")
    frame = None
    for _ in range(100):
        fl = [f for f in page.frames if f != page.main_frame]
        if fl: frame = fl[0]; break
        time.sleep(0.1)
    wait_ready(page, frame)
    t0 = time.time()
    def P(): return frame.evaluate("() => window.__leakhunt.state.pres") or {}
    def check(tag, shot):
        time.sleep(0.4)
        audit(frame, f"{name}/{tag}", fails); fails.extend(f"{name}/{tag}: {x}" for x in power_problems(frame)); snap(page, f"{name}_{shot}")
        res["parity_checks"] += 1
    def tap(label):
        frame.get_by_role("button", name=label, exact=True).click(timeout=5000); res["taps"] += 1
        time.sleep(0.8)
    p = P()
    if p.get("kind") != "status" or "never drew less than" not in p.get("speech", ""):
        fails.append(f"{name}: first screen is not the overnight power check: {p.get('kind')} {p.get('speech', '')[:80]!r}")
        res["failures"] = fails; ctx.close(); br.close(); return res
    check("status", "01_status")
    bad = frame.evaluate("() => { const s = document.createElement('p'); s.id = 'probe'; s.textContent = 'Flow 2.0 litres per minute'; document.getElementById('card-main').appendChild(s); return 1; }")
    res["broken_variant_water_words_caught"] = any("water wording" in x for x in power_problems(frame))
    frame.evaluate("() => document.getElementById('probe').remove()")
    if not res["broken_variant_water_words_caught"]: fails.append(f"{name}: the check did not notice water wording on the screen")
    tap("Find what's drawing it")
    if P().get("kind") != "choose_mode": fails.append(f"{name}: no choice of how to find it: {P().get('kind')}")
    check("choose", "02_choose")
    tap("Run it for me")
    if P().get("kind") != "needs_consent": fails.append(f"{name}: no consent question: {P().get('kind')}")
    tap("Yes, run the test")
    p = P()
    if p.get("kind") != "hunt" or not (p.get("auto") or {}).get("needs_you"): fails.append(f"{name}: the hunt did not start by asking for a quiet home")
    check("ready", "03_ready")
    tap("Nothing is in use")
    shot_mid = False
    while time.time() - t0 < 120:
        p = P(); res["polls"] += 1
        if p.get("status") != "active": break
        if not shot_mid and (p.get("auto") or {}).get("closed_by_alexa"):
            check("running", "04_running"); shot_mid = True
        time.sleep(0.5)
    p = P()
    if p.get("status") != "done" or p.get("found") != ["tv"]: fails.append(f"{name}: the hunt ended {p.get('status')} {p.get('outcome')} {p.get('found')}")
    check("result", "05_result")
    tap("Turn it back on")
    p = P()
    pos = {r["id"]: r["position"] for r in p.get("pipes") or []}
    if pos.get("tv") != "open": fails.append(f"{name}: the TV plug is not back on: {pos}")
    if pos.get("router") != "open" or pos.get("freezer") != "open": fails.append(f"{name}: a must-stay-on plug is not on: {pos}")
    check("plugs", "06_plugs")
    res.update(seconds=round(time.time() - t0, 1), steps=6)
    errs = [e for e in errs if "Failed to load resource" not in e]
    res["failures"] = fails + ([f"{name}: console/page errors: {errs[:2]}"] if errs else [])
    res["js_errors"] = errs[:3]
    ctx.close(); br.close()
    return res

def guarded(fn, name, *a, **k):
    """One scenario that crashes must not hide the others: record it as a failure and carry on."""
    try:
        return fn(*a, **k)
    except Exception as e:
        return dict(name=name, steps=0, taps=0, polls=0, parity_checks=0, seconds=0, failures=[f"{name}: crashed: {type(e).__name__}: {str(e).splitlines()[0][:240]}"])

def main():
    out = []
    only = sys.argv[2] if len(sys.argv) > 2 else None        # python3 tools/ui_check.py URL [name-substring]  (runs only matching scenarios)
    with sync_playwright() as pw:
        if not only or only in "light_768x480": out.append(guarded(run_scenario, "light_768x480", pw, "light_768x480", "light", 768, 480, shots=("start", "steps", "wait", "done"), keyboard=True, ev=True))
        if not only or only in "dark_768x480": out.append(guarded(run_scenario, "dark_768x480", pw, "dark_768x480", "dark", 768, 480, first="start_leak_hunt", args={"resume": False}, shots=("start", "steps", "done")))
        if not only or only in "light_1280x800": out.append(guarded(run_scenario, "light_1280x800", pw, "light_1280x800", "light", 1280, 800, first="start_leak_hunt", args={"resume": False}))
        if not only or only in "dark_1280x800": out.append(guarded(run_scenario, "dark_1280x800", pw, "dark_1280x800", "dark", 1280, 800, first="start_leak_hunt", args={"resume": False}))
        if not only or only in "inline_light_600x260": out.append(guarded(run_scenario, "inline_light_600x260", pw, "inline_light_600x260", "light", 600, 260, mode="inline", first="start_leak_hunt", args={"resume": False}))
        if not only or only in "phone_dark_360x640": out.append(guarded(run_scenario, "phone_dark_360x640", pw, "phone_dark_360x640", "dark", 360, 640, first="start_leak_hunt", args={"resume": False}))
        if not only or only in "auto_light_768x480": out.append(guarded(run_scenario, "auto_light_768x480", pw, "auto_light_768x480", "light", 768, 480, first="start_leak_hunt", args={"resume": False}, flow="auto", reopen=True, shots=("start", "steps", "done")))
        if not only or only in "auto_dark_1280x800": out.append(guarded(run_scenario, "auto_dark_1280x800", pw, "auto_dark_1280x800", "dark", 1280, 800, first="start_leak_hunt", args={"resume": False}, flow="auto", answer="release"))
        if not only or only in "auto_phone_light_360x640": out.append(guarded(run_scenario, "auto_phone_light_360x640", pw, "auto_phone_light_360x640", "light", 360, 640, first="start_leak_hunt", args={"resume": False}, flow="auto", reopen=True))
        if not only or only in "fill_kitchen_phone_light_360x640": out.append(guarded(run_fill_ui, "fill_kitchen_phone_light_360x640", pw, "fill_kitchen_phone_light_360x640", "light", 360, 640, outlet="kitchen tap", litres=5, start_text="Running 5 litres from the kitchen tap", end_text="came out of the kitchen tap"))      # before the pipes scenario, which leaves both kitchen pipes off (a fill then rightly refuses)
        if not only or only in "pipes_light_768x480": out.append(guarded(run_pipes, "pipes_light_768x480", pw, "pipes_light_768x480", "light", 768, 480))
        if not only or only in "fill_dark_1280x800": out.append(guarded(run_fill_ui, "fill_dark_1280x800", pw, "fill_dark_1280x800", "dark", 1280, 800))
        if not only or only in "modes_light_768x480": out.append(guarded(run_modes_ui, "modes_light_768x480", pw, "modes_light_768x480", "light", 768, 480))
        if not only or only in "power_light_768x480": out.append(guarded(run_power_ui, "power_light_768x480", pw, "power_light_768x480", "light", 768, 480))
        if not only or only in "power_phone_dark_360x640": out.append(guarded(run_power_ui, "power_phone_dark_360x640", pw, "power_phone_dark_360x640", "dark", 360, 640))
        if not only or only in "plug_dark_1280x800": out.append(guarded(run_plug, "plug_dark_1280x800", pw, "plug_dark_1280x800", "dark", 1280, 800))
        if not only or only in "challenge_dark_1280x800": out.append(guarded(run_scenario, "challenge_dark_1280x800", pw, "challenge_dark_1280x800", "dark", 1280, 800, flow="auto", challenge=True, shots=("start", "done")))
    if not only: json.dump(out, open(os.path.join(ROOT, "docs", "ui_check.json"), "w"), indent=1)
    bad = 0
    for r in out:
        print(f"{r['name']:24s} steps={r['steps']} taps={r['taps']} polls={r['polls']} parity_ok={r['parity_checks']} t={r['seconds']}s fails={len(r['failures'])}")
        for f in r["failures"]: print("   -", f); bad += 1
    print("TOTAL FAILURES", bad)

main()
