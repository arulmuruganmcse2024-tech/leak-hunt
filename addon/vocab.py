"""Words for the power hunt.

The hunt engine never knew it was water: it sees a meter that counts something, branches that can be switched off, and a
customer who answers. For electricity the meter counts watt-minutes (so its "per minute" rate is watts), the branches are smart
plugs, and the leak is an always-on draw. The engine, the safety rules and the screen are the same code; only the words change.

`to_power` rewrites the human-readable text in a presentation (never ids, kinds or other protocol fields), and `water_words` lists
any water wording left over, so a test can prove none reaches the customer.
"""
from __future__ import annotations

import re
from typing import Any, List, Tuple

# fields that carry protocol values, never prose: leave them exactly as they are
PROTOCOL_KEYS = {"kind", "status", "id", "hunt_id", "fill_id", "mode", "outcome", "phase", "url", "sha256", "hmac", "verdict",
                 "reason", "source", "position", "parent", "room", "utility", "pipe", "pipes_ids", "node", "nodes_ids", "date",
                 "boot_id", "sample_id", "valve", "want", "error", "hub_error", "source_error", "grade", "by", "action_kind"}


def _num(v: float) -> str:
    return f"{v:.0f}" if v >= 9.95 or v == 0 else f"{v:.1f}"


def _w(m: re.Match) -> str:
    return _num(float(m.group(1))) + " watts"


def _wshort(m: re.Match) -> str:
    return _num(float(m.group(1))) + " W"


# whole sentences first (the water versions name taps and cisterns), then phrases, then single words
RULES: List[Tuple[str, Any]] = [
    (r"Turn off everything that uses water\W+taps, washing machine, cistern fills\W+but leave every isolation valve open\.",
     "Turn off what you're using, like lights, the TV and chargers, but leave every smart plug on."),
    (r"make sure nothing is using water: taps, washing machine, cistern fills\.",
     "make sure nothing is in active use: lights off, nothing cooking, washing or charging."),
    (r"I've opened all my pipes", "I've switched on every plug I control"),
    (r"Tell me when it's quiet\.", "Tell me when nothing is in use."),
    (r"Measuring the quiet baseline\.", "Measuring the base draw."),
    (r"simulated house", "simulated home"),
    (r"SIMULATED HOUSE", "SIMULATED HOME"),
    (r"not real plumbing", "not real wiring"),
    (r"With everything off, (\d+(?:\.\d+)?) litres per minute is still flowing",
     r"With nothing in use, your home draws \1 litres per minute"),
    (r"so the water isn't going through it\.", "so nothing larger than that is drawing through it."),
    (r"I found a confirmed source, but some flow remains that I can't place at any valve you've told me about\.",
     "The rest of the always-on power isn't behind any plug I can switch."),
    (r"is still flowing, so there is another source\.", "is still drawn, so something else is drawing power too."),
    (r"which can't be the valve\. Someone may have used water\.", "which can't be the plug. Someone may have switched something on."),
    (r" until (it has|they have) been checked\?", "?"),
    (r" until (it has|they have) been checked\.", "."),
    (r"\bI reopened the ([\w ]+?)\.", r"I switched the \1 back on."),
    (r"pipe hub", "plug controller"),
    (r"Simulated house", "Simulated home"),
    (r"Sensor floor", "Meter floor"),
    (r"sensor floor", "meter floor"),
    (r"I close your pipes one at a time and measure what the sensor sees\.", "I switch your plugs off one at a time and measure what the energy meter sees."),
    (r"Closed by me", "Switched off by me"),
    (r"Everything is off", "Nothing is in use"),
    (r"I closed it", "I switched it off"),
    (r"I opened it", "I switched it on"),
    (r"keep it closed", "keep it off"),
    (r"\bClosed\b", "Off"),
    (r"You can open (.+?) again now\.", r"You can switch \1 back on now."),
    (r"I've put back every pipe I closed\.", "I've switched back on every plug I switched off."),
    (r"put every pipe I closed back", "switched back on every plug I switched off"),
    (r"find my leak", "find what's using power"),
    (r"Your main safety valve is closed because it detected a leak\..*?device\.",
     "Your energy meter reports the supply is off. I won't switch any plug until it's back."),
    (r"Your main safety valve closed by itself\..*?continue\.",
     "Your energy meter reports the supply is off. I can't switch it back on from here; check your main switch, then we can continue."),
    (r"until you tell me it's been repaired\. Then say", "until you ask for it back. Then say"),
    (r"once it's repaired", "when you want it back"),
    (r"Once it's repaired", "When you want it back"),
    (r"When it's repaired", "When you want it back"),
    (r"Has it been repaired, and do you want it back on\?", "Do you want it back on?"),
    (r"feeds something safety-critical, so I never switch it\.", "must stay on, so I never switch it."),
    (r"\bValves\b", "Plugs"),
    (r"\bValve\b", "Plug"),
    (r"Closed by Alexa", "Off by Alexa"),
    (r"Kept closed", "Kept off"),
    (r"Keep it closed", "Keep it off"),
    (r"Evidence for my plumber", "Evidence for my electrician"),
    (r"The flow is isolated to the water behind that valve\.", "The draw is isolated to what is plugged in behind that plug."),
    (r"Closing it stopped that flow and reopening it brought it back", "Switching it off stopped that draw and switching it back on brought it back"),
    (r"Leave the ([\w ]+?) valve closed until it's repaired\.", r"Leave the \1 off until you've dealt with what's plugged into it."),
    (r"no water moves above", "nothing is drawn above"),
    (r"Nothing else is running continuously above that limit\.", "Nothing else is drawing power continuously above that limit."),
    (r"water to them is off for a few minutes in total", "what's plugged into them is off for a few minutes in total"),
    (r"I'll close your pipes one after another until the flow stops", "I'll switch your plugs off one after another until the draw drops"),
    (r"I'll close your pipes one after another", "I'll switch your plugs off one after another"),
    (r"guide you valve by valve", "guide you plug by plug"),
    (r"what your flow sensor reads reliably", "what your energy meter reads reliably"),
    (r"what the sensor can read reliably", "what the energy meter can read reliably"),
    (r"what your sensor can read", "what your energy meter can read"),
    (r"what my sensor can read", "what my energy meter can read"),
    (r"Water has been moving all night", "Power has been drawn all night"),
    (r"is flowing through your main line", "is being drawn from your supply"),
    (r"flowing through your main line", "drawn from your supply"),
    (r"(\d+(?:\.\d+)?) litres per minute is still flowing", r"\1 litres per minute is still drawn"),
    (r"is flowing behind the ([\w ]+?) valve", r"is drawn behind the \1"),
    (r"(\d+(?:\.\d+)?) litres per minute", _w),
    (r"(\d+(?:\.\d+)?) L/min", _wshort),
    (r"litres per minute", "watts"),
    (r"L/min", "W"),
    (r"main safety valve", "main breaker"),
    (r"pipe controller", "plug controller"),
    (r"leak hunt", "power hunt"),
    (r"Leak hunt", "Power hunt"),
    (r"Leak Hunt", "Power Hunt"),
    (r"find the leak", "find what's drawing it"),
    (r"Find the leak", "Find what's drawing it"),
    (r"find a leak", "find an always-on draw"),
    (r"the leak I found there", "the always-on draw I found there"),
    (r"after I found a leak there", "after I found an always-on draw there"),
    (r"after a leak", "after an always-on draw"),
    (r"a leak", "an always-on draw"),
    (r"leaks", "always-on draws"),
    (r"leak", "always-on draw"),
    (r"plumber", "electrician"),
    (r"hand valves", "plugs I can't switch"),
    (r"hand valve", "plug I can't switch"),
    (r"isolation valves?", "smart plug"),
    (r"\bClose the ([\w ]+?) valve\b", r"Switch off the \1"),
    (r"\bOpen the ([\w ]+?) valve\b", r"Switch the \1 back on"),
    (r"the ([\w ]+?) valve", r"the \1"),
    (r"\bvalves\b", "plugs"),
    (r"\bvalve\b", "plug"),
    (r"\bpipes\b", "plugs"),
    (r"\bpipe\b", "plug"),
    (r"\bPipes\b", "Plugs"),
    (r"\bPipe\b", "Plug"),
    (r"\bI closed\b", "I switched off"),
    (r"\bI reopened\b", "I switched back on"),
    (r"\bAlexa closed\b", "Alexa switched off"),
    (r"\bAlexa opened\b", "Alexa switched on"),
    (r"\bwhen the ([\w ]+?) was closed\b", r"when the \1 was switched off"),
    (r"\bclosed\b", "off"),
    (r"\breopened\b", "back on"),
    (r"\bClosing\b", "Switching off"),
    (r"\bclosing\b", "switching off"),
    (r"\breopening\b", "switching back on"),
    (r"\bFlow\b", "Power"),
    (r"\bflowing\b", "drawn"),
    (r"\bflows\b", "draws"),
    (r"\bflow\b", "draw"),
    (r"water sensor", "energy meter"),
    (r"water meter", "energy meter"),
    (r"flow sensor", "energy meter"),
    (r"\bsensor\b", "energy meter"),
    (r"\bwater use\b", "power use"),
    (r"\bwater\b", "power"),
    (r"\bWater\b", "Power"),
    (r"Demo house \(simulated\)", "Demo home's electricity (simulated)"),
]
_COMPILED = [(re.compile(p), r) for p, r in RULES]

WATER_WORDS = re.compile(r"\b(litres?|L/min|water|pipes?|valves?|leaks?|taps?|cisterns?|plumb\w*|flow\w*|tub|bath\w*)\b", re.I)


def js_rules() -> List[List[str]]:
    """The same rules for the screen (addon/ui/console.html), as [pattern, replacement] with JavaScript's $1 for groups. A number
    formatter is sent as '@W' (watts) or '@WS' (W)."""
    out = []
    for pat, rep in RULES:
        if callable(rep):
            rep = "@W" if rep is _w else "@WS"
        else:
            rep = re.sub(r"\\(\d)", r"$\1", rep)
        out.append([pat, rep])
    return out


def translate_html(page: str) -> str:
    """Power words for a generated HTML page: text between tags and aria-labels only, never tag names, classes or styles."""
    parts = re.split(r"(<style.*?</style>|<script.*?</script>|<[^>]+>)", page, flags=re.S)
    out = []
    for part in parts:
        if part.startswith("<"):
            if not part.startswith(("<style", "<script")):
                part = re.sub(r"aria-label='([^']*)'", lambda m: "aria-label='" + translate(m.group(1)) + "'", part)
            out.append(part)
        else:
            out.append(translate(part))
    return "".join(out)


def translate(text: str) -> str:
    for rx, rep in _COMPILED:
        text = rx.sub(rep, text)
    return text


def to_power(obj: Any, key: str = "") -> Any:
    """A copy of a presentation with every human-readable string in power words."""
    if isinstance(obj, dict):
        return {k: (v if k in PROTOCOL_KEYS else to_power(v, k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_power(v, key) for v in obj]
    if isinstance(obj, str) and " " in obj:
        return translate(obj)
    return obj


def water_words(obj: Any, path: str = "") -> List[str]:
    """Every human-readable string in a presentation that still uses water wording, with where it was."""
    out: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in PROTOCOL_KEYS:
                out += water_words(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            out += water_words(v, path + "[]")
    elif isinstance(obj, str) and " " in obj and WATER_WORDS.search(obj):
        out.append(f"{path}: {obj}")
    return out
