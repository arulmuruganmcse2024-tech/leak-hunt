"""Name pipes the way a person does ("kitchen pipe 2") and resolve what they say back to a pipe.

Voice rule: when two pipes both fit what was said, never guess. `resolve` returns kind="many" and a
question to ask; the caller asks it and resolves again with the answer.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .core import Node, ROOT, Topology

_NUM_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
              "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
# Words that carry no identity. Note that "tap" is NOT here: "garden tap" is a name.
_STOP = {"the", "a", "an", "my", "our", "your", "please", "pipe", "pipes", "valve", "valves", "line", "lines", "water",
         "supply", "number", "no", "num", "for", "of", "in", "on", "off", "to", "at", "from", "and", "or", "turn",
         "switch", "open", "close", "shut", "it", "that", "this"}
_ALL = {"all", "every", "each", "both", "everything", "entire", "whole"}
_PLURAL_NOUNS = {"pipes", "valves", "lines", "taps"}
_EVERYTHING = {"all", "every", "everything", "entire", "whole"}      # "both" alone is not "everything"


def _singular(w: str) -> str:
    if w.isdigit() or len(w) <= 3:
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def words(text: str) -> List[str]:
    """Lower-case word and number tokens; '#2', 'pipe2', 'second' and 'two' all become '2'."""
    res: List[str] = []
    for t in re.findall(r"[a-z]+|\d+", (text or "").lower().replace("'s", "")):
        if t in _NUM_WORDS:
            res.append(str(_NUM_WORDS[t]))
        elif t in ("st", "nd", "rd", "th"):
            continue
        else:
            res.append(_singular(t))
    return res


def normalise(text: str) -> Tuple[Set[str], bool]:
    toks = words(text)
    raw = set(re.findall(r"[a-z]+", (text or "").lower()))
    plural = any(t in _ALL for t in toks) or bool(raw & _PLURAL_NOUNS)
    return {t for t in toks if t not in _STOP and t not in _ALL}, plural


def _name_sets(n: Node, with_id: bool = False) -> List[Set[str]]:
    room = normalise(n.room)[0]
    num = {str(n.number)} if n.number else set()
    sets = [normalise(n.label)[0] | room | num]
    for a in n.aliases:
        sets.append(normalise(a)[0] | room | num)
        sets.append(normalise(a)[0])
    if with_id:                                    # the id is a fallback, never a rival to what the customer calls it
        sets.append(normalise(n.id)[0])
    return [s for s in sets if s]


@dataclass
class Match:
    kind: str                                # one | many | none
    nodes: List[str] = field(default_factory=list)
    all_requested: bool = False              # the customer said "all" / "both" / "every"
    question: str = ""                       # what to ask (many) or say (none)
    fuzzy: bool = False                      # matched only after fixing a mis-heard word


def join_or(items: Sequence[str], word: str = "or") -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} {word} {items[1]}"
    return ", ".join(items[:-1]) + f" {word} {items[-1]}"


def describe_pipes(topo: Topology, only: Optional[Iterable[str]] = None, limit: int = 10) -> str:
    """'bathroom: toilet, shower. kitchen: kitchen pipe 1, kitchen pipe 2. garden tap.' - for 'what can I switch?'"""
    ids = list(only) if only is not None else list(topo.nodes)
    by_room: Dict[str, List[str]] = {}
    for i in ids[:limit * 3]:
        n = topo.nodes[i]
        by_room.setdefault(n.room or "", []).append(n.label)
    parts = []
    for room, labels in by_room.items():
        parts.append(f"{room}: {', '.join(labels)}" if room and len(labels) > 1 else ", ".join(labels))
    return ". ".join(parts) + "." if parts else "no pipes."


def _hits(topo: Topology, q: Set[str], pool: Sequence[str]) -> List[str]:
    hits = [i for i in pool if any(q <= s for s in _name_sets(topo.nodes[i]))]
    if not hits:
        hits = [i for i in pool if any(q <= s for s in _name_sets(topo.nodes[i], with_id=True))]
    return hits


def resolve(topo: Topology, phrase: str, *, pool: Optional[Sequence[str]] = None) -> Match:
    """Map a spoken pipe name to node ids. `pool` limits the candidates (for example, only switchable pipes)."""
    ids = list(pool) if pool is not None else list(topo.nodes)
    q, plural = normalise(phrase)
    if not q:
        every = any(t in _EVERYTHING for t in words(phrase))
        if every and ids:
            return Match("many", ids, all_requested=True)
        return Match("none", question="Which pipe? Say a room or a pipe name. " + describe_pipes(topo, ids))
    hits = _hits(topo, q, ids)
    fuzzy = False
    if not hits:                                                     # fix one mis-heard word at most, never guess two
        vocab = sorted({t for i in ids for s in _name_sets(topo.nodes[i], with_id=True) for t in s})
        fixed = set()
        changed = 0
        for t in q:
            if t in vocab or t.isdigit():
                fixed.add(t)
                continue
            c = difflib.get_close_matches(t, vocab, n=1, cutoff=0.8)
            if c:
                fixed.add(c[0])
                changed += 1
            else:
                fixed.add(t)
        if changed == 1:
            hits = _hits(topo, fixed, ids)
            fuzzy = bool(hits)
    if not hits:
        rooms = {topo.nodes[i].room for i in ids if topo.nodes[i].room}
        room_q = [r for r in rooms if normalise(r)[0] and normalise(r)[0] <= q]
        if room_q:
            same = [i for i in ids if topo.nodes[i].room == room_q[0]]
            return Match("none", question=f"I don't have a pipe called {phrase.strip()}. In the {room_q[0]} I have "
                                          f"{join_or([topo.label(i) for i in same], 'and')}.")
        return Match("none", question=f"I don't have a pipe called {phrase.strip()}. I have: {describe_pipes(topo, ids)}")
    if len(hits) == 1:
        return Match("one", hits, fuzzy=fuzzy)
    exact = [i for i in hits if normalise(topo.nodes[i].label)[0] == q]
    if len(exact) == 1 and not plural:
        return Match("one", exact, fuzzy=fuzzy)
    if plural:
        return Match("many", hits, all_requested=True, fuzzy=fuzzy)
    # a branch valve that governs every other hit is "the bathroom"
    for h in hits:
        rest = [x for x in hits if x != h]
        if all(h in topo.ancestors(x) for x in rest):
            return Match("one", [h], fuzzy=fuzzy)
    labels = [topo.label(i) for i in hits]
    q_txt = join_or(labels) + "?" + (" Or say all of them." if len(hits) > 2 else " Or say both.")
    return Match("many", hits, question="Which one: " + q_txt, fuzzy=fuzzy)


# ------------------------------------------------------------------------------------ naming a household

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "pipe"


def name_pipes(rows: Sequence[dict]) -> List[dict]:
    """Give every valve a label, a number when its room has several unnamed pipes, and a unique id.

    A row may carry: id, label, parent, room, name (what it is: 'sink'), number, aliases, protect, prior, how.
    Rows with a room but no label are called '<room> pipe 1', '<room> pipe 2' ... in the order given,
    counted per (room, parent), so a kitchen with two pipes becomes 'kitchen pipe 1' and 'kitchen pipe 2'.
    Rows that already have a label (or a name) keep it.
    """
    out = [dict(r) for r in rows]
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for r in out:
        r.setdefault("parent", ROOT)
        r["parent"] = r.get("parent") or ROOT
        r["aliases"] = list(r.get("aliases") or [])
        r["room"] = (r.get("room") or "").strip().lower()
        if not r.get("label") and r.get("name"):
            r["label"] = f"{r['room']} {r['name']}".strip() if r["room"] and r["name"].lower() not in r["room"] else r["name"]
            if r["name"] not in r["aliases"]:
                r["aliases"].append(r["name"])
        if not r.get("label") and r["room"]:
            groups.setdefault((r["room"], r["parent"]), []).append(r)
    for (room, _parent), rs in groups.items():
        used = {int(r.get("number") or 0) for r in rs if r.get("number")}
        nxt = 1
        for r in rs:
            if len(rs) > 1 and not r.get("number"):
                while nxt in used:
                    nxt += 1
                r["number"] = nxt
                used.add(nxt)
            r["label"] = f"{room} pipe {r['number']}" if r.get("number") else f"{room} pipe"
    seen: Set[str] = set()
    for r in out:
        if not r.get("label"):
            raise ValueError("every valve needs a label, a room, or a name")
        r.pop("name", None)
        if not r.get("id"):
            r["id"] = _slug(r["label"])
        base, k = r["id"], 2
        while r["id"] in seen:
            r["id"] = f"{base}-{k}"
            k += 1
        seen.add(r["id"])
        r.setdefault("prior", 1.0)
        r.setdefault("cost", 1.0)
        r.setdefault("how", "")
        r.setdefault("number", 0)
        r.setdefault("protect", False)
    return out
