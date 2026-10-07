"""HTML-twin determinism demo — DESIGN.md §5.1, Neil's 2026-09-27 rich-HTML directive.

The HTML twin is purely presentational: it must never add facts beyond the
plain-text body (the plain part is the complete message and the secrecy
gate's input — session #18 decision). This demo pins that invariant with
hermetic, deterministic checks — no network, no provider keys:

1. Round-trip fidelity: every non-blank plain-text line appears verbatim (in
   order) in the tag-stripped, entity-unescaped HTML.
2. Vocabulary: the only words in the visible HTML that are not in the plain
   body are the documented boilerplate — the <head> section (title + styles,
   stripped from the comparison) and the composite <img> alt text
   "This turn, rendered" (accessibility label, not a game fact).
3. Escaping: hostile GM/player text (<script>, tags, &, quotes) is always
   entity-escaped — never raw HTML.
4. Structure guards: dateline styling only for the true first-block dateline
   (a narrative line that merely looks like one stays a plain paragraph);
   the closer/footer/hr/section rules; the composite marker passes through
   body_to_html verbatim and is resolved only by
   mailer._resolve_composite_marker (relay edition: the attachment rides in
   the atfl_outbound envelope now, not in MIME).

All green = the twin is a deterministic, fact-faithful shadow of the plain
body, and every email type (turn, death turn, nudge, clarification) honors it.
"""
import html as _html
import json
import re
import sys

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from server.render import (render_turn_email, render_nudge, render_clarification,
                           body_to_html, DEATH_CLOSER, COMPOSITE_IMG_MARKER,
                           COMPOSITE_CID)
from server.mailer import _resolve_composite_marker
from server.turn_loop import TurnResult


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"demo failed: {name}")


def visible_text(html_body):
    """Visible rendered text: drop the <head> boilerplate (title/styles are
    presentational, not message facts), strip tags to whitespace (so <br>
    joins stay line breaks, not word merges), unescape entities."""
    no_head = re.sub(r"<head>.*?</head>", "", html_body, flags=re.S)
    stripped = re.sub(r"<[^>]+>", " ", no_head)
    return _html.unescape(re.sub(r"\s+", " ", stripped))


_STRUCTURAL = re.compile(r"^---(?: .+ ---)?$")


def is_structural(line):
    """'---' and '--- X ---' are styling syntax, not facts: they become
    <hr> / <h3> (asserted in section 4) rather than round-tripping."""
    return bool(_STRUCTURAL.match(line.strip()))


def check_roundtrip(name, text, html_body):
    vis = visible_text(html_body)
    plain_lines = [l for l in text.split("\n")
                   if l.strip() and not is_structural(l)]
    pos, ok = 0, True
    for line in plain_lines:
        at = vis.find(line.strip(), pos)
        if at < 0:
            ok = False
            break
        pos = at + len(line.strip())
    check(name, ok)


_WORD = re.compile(r"[a-z0-9]+")


def words(text):
    return set(_WORD.findall(text.lower()))


# The only non-plain-body words the HTML layer may ever introduce: the
# composite <img> alt text (accessibility label — describes the image,
# states nothing about the game world). 2026-09-27 pinned.
ALT_ALLOWLIST = {"this", "turn", "rendered"}


def view_with(slug="trailhead"):
    return {
        "places": {
            "trailhead": {"discovered": True},
            "ridge-lookout": {"discovered": False},
            slug: {"discovered": True} if slug not in ("trailhead", "ridge-lookout")
            else {"discovered": True},
        },
        "actors": {"player": {"inventory": json.dumps(
            {"hands": ["lantern"], "backpack": ["rope", "tin-cup"]})}},
    }


def turn_result(narrative, game_over=False, turn_no=3):
    return TurnResult(
        turn_id="t1", turn_no=turn_no, game_clock_start=60,
        game_clock_end=120, narrative=narrative, questions=[],
        denylist_checked=True, game_over=game_over)


GUID = "12345678-1234-1234-1234-123456789abc"


# --- 1. Round-trip fidelity on a normal turn ------------------------------
subj, text, html = render_turn_email(GUID, view_with(), turn_result(
    "While you were quiet: the fog thickened.\n\n"
    "You pick up the lantern. The trail bends where you left it."))
vis = visible_text(html)
check_roundtrip("every plain line appears verbatim, in order, in the visible HTML",
                text, html)
check("composite marker passes through body_to_html verbatim (mailer resolves it)",
      html.count(COMPOSITE_IMG_MARKER) == 1)
check("plain body carries the marker too (one template, two renderings)",
      text.count(COMPOSITE_IMG_MARKER) == 1)

# --- 2. Vocabulary: no new facts -------------------------------------------
voc = words(vis) - words(text) - ALT_ALLOWLIST
check("visible HTML words ⊆ plain-body words + alt allowlist", not voc)
check("the alt allowlist is actually exercised (tag present after substitution)",
      "alt=\"This turn, rendered\"" in
      _resolve_composite_marker(html, True))

# --- 3. Escaping: hostile content never becomes raw HTML -------------------
nasty = ("While you were quiet: nothing.\n\n"
         'A tin sign reads <b>KEEP OUT</b> & "no trespassing".\n'
         "<script>vanish()</script> — someone's joke.\n"
         "The paint smells like 5 < 6 & 7 > 2.\n"
         "Day 3 · 08:15 · morning")
_, text_n, html_n = render_turn_email(GUID, view_with(), turn_result(nasty))
check("no raw <script> from GM narrative reaches the HTML", "<script>" not in html_n)
check("no raw <b> from GM narrative reaches the HTML",
      "<b>KEEP OUT</b>" not in html_n)
check("ampersands and comparisons are entity-escaped",
      "&amp;" in html_n and "5 &lt; 6" in html_n)
check("hostile lines round-trip verbatim through strip+unescape",
      all(l.strip() in visible_text(html_n) for l in nasty.split("\n") if l.strip()))
check("a mid-narrative dateline-lookalike is NOT styled as the dateline",
      '<p class="dateline">Day 3' not in html_n)
check("the true first-block dateline IS styled",
      '<p class="dateline">Day 1 · 08:00 · morning</p>' in html_n)
check("vocabulary still holds on the hostile turn",
      not (words(visible_text(html_n)) - words(text_n) - ALT_ALLOWLIST))

# --- 4. Structure rules -----------------------------------------------------
check("--- line becomes <hr>", "<hr>" in html_n)
check("--- X --- line becomes an h3 section",
      '<h3 class="section">Known places</h3>' in html_n)
check("game-code footer lines share one mono footer paragraph",
      re.search(r'<p class="code-footer">Game code: [^<]*<br>Turn 3 ·', html_n)
      is not None)
check("death closer gets the closer class and the exact wording",
      '<p class="closer">' + _html.escape(DEATH_CLOSER) + "</p>"
      in render_turn_email(GUID, view_with(), turn_result("Gone.", game_over=True))[2])

# --- 5. Marker resolution at the mailer layer ------------------------------
# The cid the tag references must equal the envelope attachment's
# content_id (COMPOSITE_CID) — the sender renders inline images as
# cid:<content_id>. A mismatch is a broken image in the player's email.
raw = html  # from the normal turn in section 1
with_img = _resolve_composite_marker(raw, True)
check("with the composite attached: exactly one inline img tag",
      with_img.count(f'<img src="cid:{COMPOSITE_CID}"') == 1)
check("with the composite attached: marker fully resolved",
      COMPOSITE_IMG_MARKER not in with_img)
check("with the composite attached: alt text is the only new phrase",
      not (words(visible_text(with_img)) - words(text) - ALT_ALLOWLIST))
no_img = _resolve_composite_marker(raw, False)
check("without the composite: marker dropped, no broken image",
      COMPOSITE_IMG_MARKER not in no_img and "<img" not in no_img)

# --- 6. Nudge and clarification honor the same contract ---------------------
for name, (s, t, h) in {
        "nudge": render_nudge(GUID),
        "clarification": render_clarification("The Game code was missing."),
}.items():
    check_roundtrip(f"{name}: plain lines round-trip in order", t, h)
    vis = visible_text(h)
    check(f"{name}: no new words beyond the plain body",
          not (words(vis) - words(t)))
    check(f"{name}: plain body stays complete",
          len([l for l in t.split("\n") if l.strip()]) >= 4)

# --- 7. Determinism ----------------------------------------------------------
_, _, h1 = render_turn_email(GUID, view_with(), turn_result("Same."))
_, _, h2 = render_turn_email(GUID, view_with(), turn_result("Same."))
check("body_to_html is pure: same input, byte-identical output", h1 == h2)
check("email type tag is ASCII-safe in the title",
      "<title>" in h1 and _html.escape("[ATFL 12345678]") in h1)

print("\nhtml-twin determinism: all checks green")
