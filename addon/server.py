"""Leak Hunt — an Alexa+ add-on (MCP server + MCP App).

Run:  python -m addon.server            (env: LEAKHUNT_* below)
Test: tests/test_server_http.py talks to it over raw HTTP JSON-RPC (initialize -> tools/list -> tools/call).

  LEAKHUNT_DB            sqlite path (default ./leakhunt.db)
  LEAKHUNT_KEY           HMAC key for evidence packets (default: random per process; set it in production)
  LEAKHUNT_MODE          'virtual' (default, SIMULATED house); 'device' needs a real-sensor adapter this build does not include
  LEAKHUNT_SPEED         virtual mode only: time-lapse factor (the UI says so). Simulated time advances in exact one-second
                         steps, one per tick, and the ticker runs SPEED times a second
  LEAKHUNT_PUBLIC_URL    public https base URL (used in evidence links)
  LEAKHUNT_SENSOR_URL / LEAKHUNT_SENSOR_ID / LEAKHUNT_SENSOR_TOKEN   device mode (adapter not in this build)
  LEAKHUNT_CONTROL       'virtual' mode: 0 turns the simulated pipe hub off (guided hunts only). Default 1.
  LEAKHUNT_HUB_URL / LEAKHUNT_HUB_TOKEN   device mode: a Pipe Hub (docs/PIPE_HUB.md). Setting the URL is the operator's
                         decision to let Alexa switch those pipes; no tool can do it.
  LEAKHUNT_TICK_S        how often the automatic runner looks at a running hunt, in real seconds (default 1.0 / SPEED in virtual mode)
  PORT                   default 8765
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Annotated, Any, List, Literal, Optional

from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from huntkit import Config
from huntkit.pipes import name_pipes
from . import vocab
from .oauth import LeakHuntAuth
from .power import PowerService
from .service import ENGINE_VERSION, HuntService, UserError
from .store import Store

UI_URI = "ui://leakhunt/console.html"
Utility = Annotated[Optional[Literal["water", "power"]], Field(
    description="'power' for electricity: the customer's smart plugs and whole-home energy meter. Leave it out for water.")]
DEFAULT_HID = "default"
HERE = Path(__file__).parent

INSTRUCTIONS = """\
Leak Hunt finds where a continuously running water leak is, using the customer's own flow sensor. When the customer
has a pipe controller installed it can also switch their pipes on and off by name ("kitchen pipe 2"), and it can run
the whole leak test itself.

Rules you must follow:
1. Say the tool's `speech` text as written. Do not paraphrase numbers or invent findings.
2. Guided hunt: ask for ONE physical action at a time, wait for the customer to say it is done, then call report_step.
3. Automatic hunt: when start_leak_hunt says it can run the test itself, ASK the customer whether they want you to run it
   or to guide them. Pass mode="auto" and consent=true ONLY after they clearly say yes to you running it. After that the
   customer only has to say when the house is quiet; then call hunt_status about every 10 seconds and say any new
   `speech`. Never tell the customer to operate a pipe the controller can move.
4. If the customer says stop, cancel or "stop the test" at any time, call stop_leak_hunt IMMEDIATELY, without asking
   anything first. It puts every pipe back.
5. Pipes: use the names list_pipes gives. If set_pipe asks which pipe, read the question out, wait for the answer, call
   again. Switch only what the customer asked for. Pass confirm=true ONLY after the customer said yes to a question
   that the tool itself asked. Pass all_in_room=true when they say "all" or "both".
   When a finished hunt's `speech` asks whether to KEEP a pipe closed (the pipe is held for a short time, only so the
   customer can answer), read the question out and wait. Only if the customer clearly says yes, call set_pipe for that pipe
   with state "off"; if they say no, call it with state "on". If they do not answer, do nothing: the pipe reopens by itself
   when the time in `speech` runs out. Never keep a pipe closed that the customer has not said yes to.
6. Never claim there is no leak. When the tool says nothing was measured above a limit, say that limit.
7. Never offer to open, close or re-arm the main safety valve (the device's own whole-house cut-off); this service cannot, and must not, control it. It is
   re-armed only at the device.
8. If a measurement is running, tell the customer how long and call hunt_status after that.
9. Everything is labelled simulated when `simulated` is true; say so if asked where the data comes from.
10. Filling: when the customer asks to fill the bath, fill a pot at the kitchen tap, water the plants, or fill from any other tap
    fill_water knows, call fill_water with the tap as they named it and the amount in litres they asked for; with no amount it uses
    that tap's saved amount. If fill_water asks which tap, ask the customer; never pick one for them. Say its `speech`, then call fill_status about every 15 seconds and say
    any new `speech`. If they say stop at any time, call stop_filling IMMEDIATELY. Never promise a water temperature: the fill
    controls only how much water flows. If a finished fill asks whether to find where other water is going and they say yes,
    call start_leak_hunt.
11. House modes: "sleep mode", "good night" or "bedtime" call house_mode with mode "sleep"; "away mode", "work mode", "I'm leaving
    for work" or "vacation" call it with mode "away"; it answers with what it will switch off and asks. Say its question, and call house_mode again with confirm=true ONLY after a spoken yes. Then call
    house_mode with no mode every few seconds until the meter check is in, and say its `speech`. "I'm up", "I'm home", "good
    morning" call house_mode with mode "home": that turns back on only what the mode turned off, so no confirmation is needed.
12. Power: when the customer asks what is using power at night, about always-on or standby power, or a high electricity bill, call
    water_status with utility="power". If it offers to find what's drawing it and they say yes, call start_leak_hunt with
    utility="power", and follow rules 1 to 8 with plugs instead of pipes: the same questions, the same consent, the same stop. Pass
    utility="power" to list_pipes and set_pipe for plugs ("turn off the desk plug"). Plugs the household marked must-stay-on (a
    router, a freezer) are never switched; never offer to.
"""


class Valve(BaseModel):
    id: Optional[str] = Field(None, description="Short unique id, lowercase, e.g. 'toilet'. Optional; made from the name if omitted. Needed only if another valve names this one as its parent")
    label: Optional[str] = Field(None, description="What the customer calls it, e.g. 'toilet'. Optional if you give room (and name)")
    room: Optional[str] = Field(None, description="The room it serves, e.g. 'kitchen'. Several unnamed pipes in one room are numbered automatically: 'kitchen pipe 1', 'kitchen pipe 2'")
    name: Optional[str] = Field(None, description="What it is, e.g. 'sink' or 'dishwasher'; becomes part of the label and an alias")
    number: int = Field(0, description="Pipe number within its room (1, 2, ...). Leave 0 to number automatically")
    aliases: List[str] = Field(default_factory=list, description="Other names the customer uses for it")
    protect: bool = Field(False, description="True if it feeds a water heater, boiler or sprinkler: software then never switches it")
    parent: Optional[str] = Field(None, description="Id of the branch valve this one sits behind; omit if it hangs directly off the main line")
    prior: float = Field(1.0, description="Relative likelihood that the leak is here (higher = checked earlier)")
    how: str = Field("", description="Where to find the valve, e.g. 'under the cistern'")


def _result(pres: dict, text: Optional[str] = None, *, ui: bool = True, error: bool = False) -> CallToolResult:
    meta = {"ui": {"resourceUri": UI_URI}} if ui else None
    return CallToolResult(content=[TextContent(type="text", text=text if text is not None else pres.get("speech", ""))],
                          structured_content=pres, meta=meta, is_error=error)


def _error(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)], structured_content={"kind": "error", "speech": msg},
                          is_error=True)


def _spoken(p: dict) -> str:
    """What the assistant says. The screen shows the very same `speech` string (spoken/displayed parity)."""
    return p.get("speech", "")


def build_server(svc: HuntService, *, hid_for_request=None, auth=None, token_verifier=None, auth_provider=None,
                 devhost: bool = False, oauth: Optional[LeakHuntAuth] = None) -> MCPServer:
    apps = Apps()
    html = (HERE / "ui" / "console.html").read_text(encoding="utf-8").replace("[/*POWER_RULES*/]", json.dumps(vocab.js_rules()))
    apps.add_html_resource(UI_URI, html, name="Leak Hunt console", title="Leak Hunt",
                           description="Guided leak hunt: next step, valve map and findings.",
                           csp=ResourceCsp(connect_domains=[], resource_domains=[]), prefers_border=False)

    if oauth is not None:
        from mcp.server.auth.middleware.auth_context import get_access_token
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
        auth = AuthSettings(issuer_url=oauth.issuer, resource_server_url=oauth.resource, validate_token_resource=True,
                            client_registration_options=ClientRegistrationOptions(enabled=False),
                            revocation_options=RevocationOptions(enabled=True))
        auth_provider = oauth

        def hid_for_request():                      # noqa: F811  (every call is scoped to the token's household)
            tok = get_access_token()
            if not tok or not tok.subject:
                raise UserError("Please link your home in the Alexa app first.")
            return tok.subject

    def hid() -> str:
        return hid_for_request() if hid_for_request else DEFAULT_HID

    def guard(fn):
        try:
            return fn()
        except UserError as e:
            return _error(str(e))

    power: Optional[HuntService] = getattr(svc, "power", None)      # addon/power.py: smart plugs + a whole-home energy meter

    def pick(utility: Optional[str]) -> HuntService:
        if utility == "power":
            if power is None:
                raise UserError("Power Hunt isn't set up in this home.")
            return power
        return svc

    def for_hunt(utility: Optional[str], hunt_id: Optional[str] = None) -> HuntService:
        """The service a hunt belongs to: as named, by its id, or the one with a hunt running (water first)."""
        if utility:
            return pick(utility)
        if hunt_id:
            return power if (power is not None and hunt_id.startswith(power.hunt_prefix)) else svc
        if power is not None:
            h, now = hid(), svc.clock()
            wa, pa = svc._active_hunt(h, now), power._active_hunt(h, now)
            if wa or pa:
                return svc if wa else power
            lw, lp = svc.store.latest_hunt(h), power.store.latest_hunt(h)      # nothing running: the most recent hunt
            if lp and (not lw or lp["started"] > lw["started"]):
                return power
        return svc


    # ------------------------------------------------------------------ read-only
    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="water_status", title="Water status",
               description="Is water flowing right now, and has water been moving overnight when nobody was using it? "
                           "Call this first when the customer mentions a leak, a high water bill, or a running toilet. With "
                           "utility='power': what the home draws all night and right now (always-on power), for questions about "
                           "standby power or a high electricity bill.",
               annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def water_status(utility: Utility = None) -> CallToolResult:
        return guard(lambda: _result(pick(utility).water_status(hid())))

    # ------------------------------------------------------------------ the hunt
    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="start_leak_hunt", title="Start leak hunt",
               description="Start (or resume) a hunt for a continuously running leak. If the customer has a pipe controller, the "
                           "first call (no mode) asks whether Alexa should run the whole test itself or guide them: read that "
                           "question out, and call again with mode='auto' (and consent=true, ONLY after they clearly said yes) "
                           "or mode='guided'. Pass `suspect` if they already have a guess, e.g. ['toilet']. With utility='power' "
                           "it finds which smart plug an always-on draw is behind, the same way.",
               annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def start_leak_hunt(suspect: Optional[List[str]] = None, resume: bool = True,
                        mode: Optional[Literal["auto", "guided"]] = None, consent: bool = False,
                        utility: Utility = None) -> CallToolResult:
        return guard(lambda: (lambda p: _result(p, _spoken(p)))(pick(utility).start_hunt(hid(), suspect=suspect, resume=resume, mode=mode, consent=consent)))

    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="stop_leak_hunt", title="Stop the hunt",
               description="Stop the leak hunt right now and put back every pipe it closed. Call this the moment the customer says "
                           "stop, cancel, or 'stop the test' - do not ask anything first. With no utility it stops any hunt that is "
                           "running, water or power.",
               annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def stop_leak_hunt(utility: Utility = None) -> CallToolResult:
        def go():
            if utility:
                p = pick(utility).stop_hunt(hid())
            else:                                              # "stop" with no detail stops whatever is running, water and power
                h, now = hid(), svc.clock()
                p = None
                if power is not None and power._active_hunt(h, now):
                    p = power.stop_hunt(h)
                if p is None or svc._active_hunt(h, now):
                    w = svc.stop_hunt(h)
                    p = w if p is None or w.get("kind") == "stopped" and w.get("hunt_id") is None else dict(w, speech=w["speech"] + " " + p["speech"])
            return _result(p, _spoken(p))
        return guard(go)

    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="list_pipes", title="My pipes",
               description="List the customer's pipes by room and number (for example 'kitchen pipe 1'), which are on or off, and "
                           "which ones can be switched by voice. Call it when they ask what pipes they have or what is on or off. "
                           "utility='power' lists their smart plugs instead.",
               annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def list_pipes(utility: Utility = None) -> CallToolResult:
        return guard(lambda: (lambda p: _result(p, _spoken(p)))(pick(utility).list_pipes(hid())))

    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="set_pipe", title="Switch a pipe",
               description="Turn a pipe on (water flows) or off (water stopped) by name: 'kitchen pipe 2', 'the dishwasher', "
                           "'bathroom'. If the name fits more than one pipe the tool asks which: read its question out and call "
                           "again with the answer. Pass all_in_room=true when the customer says 'all' or 'both'. Pass confirm=true "
                           "ONLY after the customer said yes to a confirmation question this tool asked. Never works on the "
                           "main safety valve. If a finished hunt is holding a pipe off and asks whether to keep it closed, call this with "
                           "state='off' ONLY after the customer clearly said to keep it off, or state='on' if they want it back on. "
                           "utility='power' switches a smart plug instead (a name with 'plug' in it is always a plug).",
               annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def set_pipe(pipe: str, state: Literal["on", "off"], all_in_room: bool = False, confirm: bool = False,
                 utility: Utility = None) -> CallToolResult:
        if utility is None and power is not None and re.search(r"\b(plug|socket)s?\b", pipe, re.I):
            utility = "power"                                 # "turn off the desk plug": a plug is never a pipe
        return guard(lambda: (lambda p: _result(p, _spoken(p)))(pick(utility).set_pipe(hid(), pipe, state, all_in_room=all_in_room, confirm=confirm)))

    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="report_step", title="Report a step",
               description="Tell the hunt what the customer just did: 'quiet' (everything is off), 'close' or 'open' a valve "
                           "(give its name in `valve`), or 'undo' (they did not actually do the last step). Returns the next step.",
               annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def report_step(kind: Literal["quiet", "close", "open", "undo"], valve: Optional[str] = None,
                    hunt_id: Optional[str] = None, utility: Utility = None) -> CallToolResult:
        return guard(lambda: (lambda p: _result(p, _spoken(p)))(for_hunt(utility, hunt_id).report(hid(), hunt_id, kind, valve)))

    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="hunt_status", title="Hunt status",
               description="The current state of the hunt. Call it after a measurement wait, or whenever the customer asks "
                           "'what now?' or 'did you find it?'.",
               annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def hunt_status(hunt_id: Optional[str] = None, utility: Utility = None) -> CallToolResult:
        return guard(lambda: (lambda p: _result(p, _spoken(p)))(for_hunt(utility, hunt_id).hunt_status(hid(), hunt_id)))

    # ------------------------------------------------------------------ filling by volume (addon/fill.py)
    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="fill_water", title="Fill water",
               description="Fill a number of litres from a tap that has an outlet valve on the customer's pipe controller: the bath, the "
                           "kitchen tap ('fill the pot, 3 litres'), the garden hose ('water the plants'). `outlet` is the tap as the "
                           "customer named it; if it is empty or matches more than one tap, the reply asks which. Without `litres` it "
                           "uses that tap's saved amount. The tap closes by itself at "
                           "the amount, and the controller closes it on its own timer if it stops hearing from the service. It does not "
                           "set the water temperature.",
               annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def fill_water(outlet: str = "", litres: Optional[float] = None) -> CallToolResult:
        return guard(lambda: (lambda p: _result(p, _spoken(p)))(svc.start_fill(hid(), outlet, litres)))

    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="stop_filling", title="Stop filling",
               description="Stop the fill right now and close the tap. Call this the moment the customer says stop - do not ask first.",
               annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def stop_filling() -> CallToolResult:
        return guard(lambda: (lambda p: _result(p, _spoken(p)))(svc.stop_fill(hid())))

    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="fill_status", title="Fill status",
               description="How the current or last fill is going: litres so far, time to go, and how it ended.",
               annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def fill_status(fill_id: Optional[str] = None) -> CallToolResult:
        return guard(lambda: (lambda p: _result(p, _spoken(p)))(svc.fill_status(hid(), fill_id)))

    # ------------------------------------------------------------------ house modes (addon/modes.py)
    @apps.tool(resource_uri=UI_URI, visibility=["model", "app"], name="house_mode", title="House mode",
               description="Sleep mode, away mode, or back home. `mode`: 'sleep' (every pipe the controller can switch goes off except "
                           "the bathroom and protected pipes), 'away' (all of them except protected pipes), 'home' (turn back on exactly "
                           "what the mode turned off). Without `confirm` it only says what it would switch and asks; pass confirm=true "
                           "only after the customer said yes. After switching it checks the meter and says whether water is still "
                           "moving. With no `mode` it reports the current mode and that check. Refuses during a leak hunt or a fill.",
               annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def house_mode(mode: str = "", confirm: bool = False) -> CallToolResult:
        return guard(lambda: (lambda p: _result(p, _spoken(p)))(svc.house_mode(hid(), mode, confirm)))

    kwargs: dict[str, Any] = {}
    if auth is not None:
        kwargs.update(auth=auth)
    if token_verifier is not None:
        kwargs.update(token_verifier=token_verifier)
    if auth_provider is not None:
        kwargs.update(auth_server_provider=auth_provider)
    mcp = MCPServer("Leak Hunt", title="Leak Hunt", instructions=INSTRUCTIONS, version="0.1.0",
                    extensions=[apps], **kwargs)

    @mcp.tool(name="night_flow", title="Overnight flow",
              description="Compare the quietest overnight water flow with the household's own previous nights.",
              annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def night_flow(utility: Utility = None) -> CallToolResult:
        return guard(lambda: _result(pick(utility).night_flow(hid()), ui=False))

    @mcp.tool(name="evidence_packet", title="Evidence packet",
              description="Write up a finished hunt as an evidence packet with integrity codes (it can be checked for changes and replayed "
                          "to the same result; it is a record of the measurements, not proof the diagnosis is physically true). "
                          "Returns a link and a checksum.",
              annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def evidence_packet(hunt_id: Optional[str] = None, utility: Utility = None) -> CallToolResult:
        def go():
            s_ = for_hunt(utility, hunt_id) if (utility or hunt_id) else pick(None)
            pk = s_.evidence(hid(), hunt_id)
            url = s_.evidence_url(pk["hunt_id"])
            res = pk["result"]
            summary = [c["text"] for c in res["claims"] if c["level"] in ("confirmed", "warning", "info")][-2:]
            pres = dict(kind="evidence", hunt_id=pk["hunt_id"], simulated=pk["simulated"], sha256=pk["sha256"],
                        hmac_sha256=pk["hmac_sha256"], url=url, outcome=pk["outcome"], summary=summary, assumptions=pk["assumptions"],
                        speech="I've written up the evidence for your plumber. " + (
                            "It's in your Alexa app." if url else "Ask me to show it on screen."))
            if s_ is power:
                pres = dict(vocab.to_power(pres), utility="power")
            return _result(pres, ui=False)
        return guard(go)

    @mcp.tool(name="set_household", title="Describe my plumbing",
              description="Tell the service which isolation valves the house has and how they nest, so the hunt can use them. "
                          "Each valve may name a parent branch valve it sits behind.",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def set_household(valves: List[Valve], name: str = "My home", timezone_offset_minutes: int = 330) -> CallToolResult:
        def go():
            rows = name_pipes([dict(id=v.id, label=v.label, room=v.room, name=v.name, number=v.number, aliases=v.aliases,
                                    protect=v.protect, parent=v.parent, prior=v.prior, how=v.how) for v in valves])
            ids, by_label = {r["id"] for r in rows}, {r["label"].lower(): r["id"] for r in rows}
            for r in rows:
                if r["parent"] not in ids and r["parent"] != "house":
                    r["parent"] = by_label.get(str(r["parent"]).lower(), r["parent"])
            try:
                h = svc.set_household(hid(), name, rows, tz_min=timezone_offset_minutes)   # never touches the pipe-hub setting
            except ValueError as e:
                raise UserError(f"That plumbing layout doesn't work: {e}")
            return _result(dict(kind="household", speech=f"Got it. I know {len(rows)} valves in {h['name']}: "
                                                          + ", ".join(r["label"] for r in rows) + ".",
                                simulated=False, valves=rows), ui=False)
        return guard(go)

    # ------------------------------------------------------------------ plain HTTP routes
    if oauth is not None:
        @mcp.custom_route("/login", methods=["GET"])
        async def login_get(request: Request) -> Response:
            return await oauth.login_get(request)

        @mcp.custom_route("/login", methods=["POST"])
        async def login_post(request: Request) -> Response:
            return await oauth.login_post(request)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True, "engine": ENGINE_VERSION})

    def _evidence_gate(request: Request):
        hunt_id = request.path_params["hunt_id"].removesuffix(".json")
        if not secrets.compare_digest(request.query_params.get("k", ""), svc.evidence_token(hunt_id)):
            return None, PlainTextResponse("Not found", status_code=404)
        s_ = power if (power is not None and hunt_id.startswith(power.hunt_prefix)) else svc
        hunt = s_.store.hunt(hunt_id)
        if not hunt:
            return None, PlainTextResponse("Not found", status_code=404)
        return (s_, hunt["hid"], hunt_id), None

    if devhost:          # a stand-in for the host page, for screenshots and tests only; never enabled in production
        @mcp.custom_route("/devhost", methods=["GET"])
        async def devhost_page(request: Request) -> Response:
            return HTMLResponse((HERE / "ui" / "devhost.html").read_text(encoding="utf-8"))

        from .challenge import Challenge             # "Try to fool it": hidden leak + chaos + reveal (see addon/challenge.py)
        challenge = Challenge(svc, DEFAULT_HID)

        async def _challenge_call(request: Request, fn) -> Response:
            host = request.client.host if request.client else ""
            if host not in ("127.0.0.1", "::1", "localhost", "testclient") and os.environ.get("LEAKHUNT_CHALLENGE_ANY_HOST") != "1":
                return JSONResponse({"error": "Challenge mode answers only to the machine it runs on."}, status_code=403)
            try:
                body = await request.json() if request.method == "POST" and (await request.body()) else {}
                return JSONResponse(fn(body if isinstance(body, dict) else {}), headers={"Cache-Control": "no-store"})
            except UserError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            except (TypeError, ValueError):
                return JSONResponse({"error": "Bad request."}, status_code=400)

        @mcp.custom_route("/challenge/new", methods=["POST"])
        async def challenge_new(request: Request) -> Response:
            return await _challenge_call(request, lambda b: challenge.new_round(b.get("seed"), b.get("script")))

        @mcp.custom_route("/challenge/chaos", methods=["POST"])
        async def challenge_chaos(request: Request) -> Response:
            return await _challenge_call(request, lambda b: challenge.act(str(b.get("action", "")), b.get("arg")))

        @mcp.custom_route("/challenge/state", methods=["GET"])
        async def challenge_state(request: Request) -> Response:
            return await _challenge_call(request, lambda b: challenge.state())

        @mcp.custom_route("/challenge/reveal", methods=["GET"])
        async def challenge_reveal(request: Request) -> Response:
            return await _challenge_call(request, lambda b: challenge.reveal())

        @mcp.custom_route("/challenge/hub", methods=["GET"])
        async def challenge_hub(request: Request) -> Response:
            return await _challenge_call(request, lambda b: challenge.controller())

    @mcp.custom_route("/evidence/{hunt_id}", methods=["GET"])
    async def evidence_html(request: Request) -> Response:
        got, err = _evidence_gate(request)
        if err:
            return err
        s_, h_, id_ = got
        pk = s_.evidence(h_, id_)
        if request.path_params["hunt_id"].endswith(".json"):
            return JSONResponse(pk)
        return HTMLResponse(s_.evidence_html(pk), headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    return mcp


class SteppedClock:
    """Virtual mode: simulated time moves only when the ticker moves it, by exactly one second per tick.

    A wall-clock multiplier produced sub-second phases between the sensor's samples, the valve acknowledgements and the person's
    taps. Every check in this repository runs on a whole-second timeline, and a hunt can take a different path on a fractional one
    (for example a window that is exactly 9 s long is long enough on a whole-second timeline and one reading short at 8.7 s), so the
    time-lapse dev host uses the same whole-second timeline as the checks."""

    def __init__(self, t0: float):
        self.t = float(t0)

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float = 1.0) -> None:
        self.t += s


def service_from_env() -> HuntService:
    mode = os.environ.get("LEAKHUNT_MODE", "virtual")
    key = os.environ.get("LEAKHUNT_KEY")
    if not key:
        key = secrets.token_hex(16)
    speed = float(os.environ.get("LEAKHUNT_SPEED", "1"))
    if mode == "virtual":
        clock = SteppedClock(float(int(time.time())))
    else:
        if speed != 1.0:
            raise SystemExit("LEAKHUNT_SPEED is for the simulated house only.")
        clock = time.time
    svc = HuntService(Store(os.environ.get("LEAKHUNT_DB", "leakhunt.db")), clock, Config(), key=key.encode(),
                      public_base_url=os.environ.get("LEAKHUNT_PUBLIC_URL", ""), time_scale=speed)
    if mode == "virtual":
        control = os.environ.get("LEAKHUNT_CONTROL", "1") != "0"
        db = os.environ.get("LEAKHUNT_DB", "leakhunt.db")
        svc.power = PowerService(Store(db + ".power" if db != ":memory:" else db), clock, key=key.encode(),   # its own store:
                                 public_base_url=os.environ.get("LEAKHUNT_PUBLIC_URL", ""), time_scale=speed)  # never shares a hunt
        svc.power.ensure_demo_household(DEFAULT_HID, control=control)
        svc.ensure_demo_household(DEFAULT_HID, control=control, outlets=control)
        svc.configure_pipehub(DEFAULT_HID, {"kind": "virtual", "enabled": True, "pipes": list(svc._topology(DEFAULT_HID).nodes),
                                            "outlets": [o["id"] for o in svc.household(DEFAULT_HID)["source"].get("outlets") or []]}
                              if control else None)
    else:
        from huntkit.sim import demo_topology
        if not svc.store.household(DEFAULT_HID):
            svc.set_household(DEFAULT_HID, "My home", demo_topology().to_json(), source=dict(
                kind="sensor", base_url=os.environ["LEAKHUNT_SENSOR_URL"], device_id=os.environ["LEAKHUNT_SENSOR_ID"],
                token_env="LEAKHUNT_SENSOR_TOKEN"))
        hub_url = os.environ.get("LEAKHUNT_HUB_URL")         # the operator's decision, and only theirs
        svc.configure_pipehub(DEFAULT_HID, {"kind": "rest", "enabled": True, "base_url": hub_url,
                                            "token_env": "LEAKHUNT_HUB_TOKEN"} if hub_url else None)
    return svc


def start_ticker(svc: HuntService, interval_s: float = 1.0, stop=None):
    """The automatic runner's heartbeat: a hunt keeps moving whether or not anyone is talking to Alexa.
    On the simulated house's stepped clock each beat also moves simulated time on by one second."""
    import logging
    import threading
    log = logging.getLogger("leakhunt.ticker")

    stepped = svc.clock if isinstance(svc.clock, SteppedClock) else None

    def loop():
        while stop is None or not stop.is_set():
            try:
                if stepped is not None:
                    stepped.advance(1.0)
                svc.tick()
                if getattr(svc, "power", None) is not None:
                    svc.power.tick()
            except Exception:                                  # one bad tick must never stop the watchdog
                log.exception("tick failed")
            time.sleep(interval_s)

    t = threading.Thread(target=loop, name="leakhunt-ticker", daemon=True)
    t.start()
    return t


def oauth_from_env(svc: HuntService) -> Optional[LeakHuntAuth]:
    """LEAKHUNT_OAUTH=1 turns on account linking (requires LEAKHUNT_PUBLIC_URL, CLIENT_ID/SECRET, REDIRECTS, PAIRING)."""
    if os.environ.get("LEAKHUNT_OAUTH") != "1":
        return None
    pairing = {}
    for item in os.environ.get("LEAKHUNT_PAIRING", "").split(","):
        if ":" in item:
            code, hid_ = item.split(":", 1)
            pairing[code.strip()] = hid_.strip()
    if not pairing:
        raise SystemExit("LEAKHUNT_PAIRING must list at least one CODE:household pair")
    return LeakHuntAuth(svc.store, issuer_url=os.environ["LEAKHUNT_PUBLIC_URL"], client_id=os.environ["LEAKHUNT_OAUTH_CLIENT_ID"],
                        client_secret=os.environ["LEAKHUNT_OAUTH_CLIENT_SECRET"],
                        redirect_uris=[u for u in os.environ["LEAKHUNT_OAUTH_REDIRECTS"].split(",") if u], pairing=pairing)


def main() -> None:
    import uvicorn
    svc = service_from_env()
    stepped = isinstance(svc.clock, SteppedClock)
    default_tick = max(0.02, 1.0 / max(svc.time_scale, 1.0)) if stepped else 1.0
    start_ticker(svc, float(os.environ.get("LEAKHUNT_TICK_S", str(default_tick))))
    app = build_server(svc, devhost=os.environ.get("LEAKHUNT_DEVHOST") == "1",
                       oauth=oauth_from_env(svc)).streamable_http_app(json_response=True, host="0.0.0.0")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8765")), log_level="info")


if __name__ == "__main__":
    main()
