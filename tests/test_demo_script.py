"""The film's script (docs/DEMO.md) fits the rules and can be said calmly: slots run back to back from 0:00, end by 2:57 (the
rules allow under 3:00), and no slot asks for more than 3.0 spoken words a second, counted over the slot (while actually talking
the synthesised voices average about 2.9 words a second, Alexa's lines up to 3.6; earlier drafts, written for a human voice-over,
used 2:52 and 2.5). Quoted words in the voice column are the words that are said."""
import os, re, unittest

DEMO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "DEMO.md")
MAX_WORDS_PER_S = 3.0


def slots(text):
    out = []
    for line in text.splitlines():
        m = re.match(r"\| (\d):(\d\d)-(\d):(\d\d) \|", line)
        if not m:
            continue
        a = int(m.group(1)) * 60 + int(m.group(2))
        b = int(m.group(3)) * 60 + int(m.group(4))
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        words = sum(len(q.split()) for q in re.findall(r'"([^"]+)"', cells[2]))
        out.append((a, b, words))
    return out


def problems(text):
    s, bad = slots(text), []
    if not s:
        return ["no shot table"]
    if s[0][0] != 0:
        bad.append("does not start at 0:00")
    for (a1, b1, _), (a2, b2, _) in zip(s, s[1:]):
        if a2 != b1:
            bad.append(f"gap or overlap at {b1} s")
    if s[-1][1] > 177:
        bad.append(f"ends at {s[-1][1]} s, after 2:57")
    for a, b, w in s:
        if b <= a:
            bad.append(f"slot at {a} s has no length")
        elif w / (b - a) > MAX_WORDS_PER_S:
            bad.append(f"slot {a}-{b} s asks for {w} words ({w / (b - a):.1f} a second)")
    return bad


class DemoScript(unittest.TestCase):
    def test_the_script_fits_and_can_be_said_calmly(self):
        self.assertEqual(problems(open(DEMO, encoding="utf-8").read()), [])

    def test_a_crowded_slot_is_noticed(self):
        text = open(DEMO, encoding="utf-8").read()
        crowded = re.sub(r'(\| \d:\d\d-\d:\d\d \|[^|]*\|)([^|]*)\|', lambda m: m.group(1) + ' "' + "word " * 60 + '" |', text, count=1)
        self.assertNotEqual(crowded, text)
        self.assertTrue(any("asks for 60 words" in p for p in problems(crowded)))

    def test_a_script_that_runs_long_is_noticed(self):
        text = open(DEMO, encoding="utf-8").read()
        last = re.findall(r"^\| (\d:\d\d)-(\d:\d\d) \|", text, flags=re.M)[-1]
        longer = text.replace(f"| {last[0]}-{last[1]} |", f"| {last[0]}-3:02 |")
        self.assertTrue(any("after 2:57" in p for p in problems(longer)))


if __name__ == "__main__":
    unittest.main()
