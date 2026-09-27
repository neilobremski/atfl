"""Turn-email renderer — DESIGN.md §5 (renderer spec).

Fills the §5.2 block order verbatim (template lives in
prototype/turn_email_layout.txt; this module builds each block from
the filtered world view + TurnResult). Receives the FILTERED view —
hidden_traits never reaches here.

Every outbound game email goes out as multipart/alternative:
text/plain + text/html (Neil's 2026-09-27 directive — rich HTML is the
readable layer). The plain part always carries the complete message;
the HTML twin is purely presentational and must never add facts.

System emails (§5.3) are never in-character: clarification (fresh
thread) and the death closer live here too. The standalone-nudge
fallback (≥24h, mutates nothing, §2.3) is mailer-level: dispatch
sends the idle turn as the daily touch instead, and the nudge is only
needed if a game somehow produces no turn email in 24h — recorded as
a mailer detail, not built here.

The Phase 3 composite, when attached, is INLINE: the mailer marks the
JPEG part with Content-ID <turn-composite> and substitutes
COMPOSITE_IMG_MARKER in the HTML with an <img src="cid:..."> tag, so
Gmail renders it inside the email body. When no composite attaches,
the marker is dropped (no broken image).
"""
import html as _html
import json
import re

DEATH_CLOSER = "This was your last email. The game is over."
TIME_OF_DAY_START_MIN = 420  # DESIGN §3.1: game_clock_min=0 <=> 07:00 local

# 2026-09-27 (Neil): no open prompt. Turn emails end with the world
# blocks; a player who does nothing just gets the next turn's
# catch-up line. OPEN_PROMPT is deleted, not deprecated.
COMPOSITE_CID = "turn-composite"
COMPOSITE_IMG_MARKER = "[[TURN_COMPOSITE]]"
COMPOSITE_IMG_TAG = (
    '<figure class="turn-img">'
    '<img src="cid:turn-composite" alt="This turn, rendered">'
    "</figure>"
)

_HTML_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>
body {{ margin:0; padding:24px; background:#fbfaf7; color:#23262b;
       font-family:Georgia,'Times New Roman',serif;
       font-size:16px; line-height:1.65; }}
.wrap {{ max-width:40em; margin:0 auto; }}
.dateline {{ color:#8a8f98; font-family:-apple-system,'Segoe UI',sans-serif;
             font-size:0.82em; letter-spacing:0.06em; text-transform:uppercase; }}
h3.section {{ font-family:-apple-system,'Segoe UI',sans-serif; font-size:0.9em;
              letter-spacing:0.08em; text-transform:uppercase; color:#6b5d3f;
              margin:1.8em 0 0.4em; }}
hr {{ border:0; border-top:1px solid #ddd6c4; margin:1.6em 0; }}
.code-footer {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
                font-size:0.82em; color:#7a7f88; }}
.closer {{ font-weight:bold; }}
figure.turn-img {{ margin:1.4em 0; }}
figure.turn-img img {{ max-width:100%; height:auto; display:block;
                       border-radius:4px; }}
p {{ margin:0 0 1em; }}
</style></head>
<body><div class="wrap">
{blocks}
</div></body></html>"""

_DATELINE_RE = re.compile(r"^Day \d+ · \d{2}:\d{2} · \w+$")


def clock_to_day_time(game_clock_min):
    """game_clock_min -> (day_n, HH:MM, time-of-day name)."""
    day_n = game_clock_min // 1440 + 1
    local_min = (TIME_OF_DAY_START_MIN + game_clock_min) % 1440
    hh, mm = divmod(local_min, 60)
    hour = local_min // 60
    tod = ("night" if hour < 5 or hour >= 21 else
           "morning" if hour < 11 else
           "midday" if hour < 14 else
           "afternoon" if hour < 18 else
           "evening")
    return day_n, f"{hh:02d}:{mm:02d}", tod


def split_catchup(narrative):
    """The GM composes the narrative WITH the catch-up lead (§2.4); the
    template shows it as a separate block (§5.2), so split it off."""
    lead, sep, rest = narrative.partition("\n\n")
    if lead.startswith("While you were quiet:"):
        return lead, rest.strip() or narrative
    return "", narrative


def render_map(view, player_location_slug):
    """§5.4 map-as-text: discovered places in full, visible-but-
    undiscovered as one-line hints. Places rows exist only for
    discovered-or-visible places — anything never rendered yet is not
    in the table at all, so every discovered=0 row is a hint."""
    lines = []
    for slug, p in view["places"].items():
        if p["discovered"]:
            where = " (where you are)" if slug == player_location_slug else ""
            lines.append(f"{slug}{where}")
        else:
            lines.append(f"{slug} — not yet explored")
    return "\n".join(lines) or "(no known places)"


def render_inventory(view):
    """Fixed-slot inventory line: hands[2] + backpack[8] (§3.2)."""
    inv = json.loads(view["actors"]["player"]["inventory"])
    hands = [i for i in inv.get("hands", []) if i]
    pack = [i for i in inv.get("backpack", []) if i]
    hand_s = ", ".join(hands) if hands else "empty"
    pack_s = ", ".join(pack) if pack else "empty"
    return f"hands: {hand_s} · backpack: {pack_s}"


def body_to_html(title, body):
    """Plain body text -> styled HTML twin, per the 2026-09-27 directive.

    Purely presentational: paragraphs, --- rules, section headings,
    the mono footer, the closer. Never adds facts — the plain part is
    the complete message. The composite marker line passes through
    verbatim; the mailer substitutes or drops it.

    Line-aware (not paragraph-aware): the text bodies join special
    lines with single newlines, so recognition happens per line and
    only run-of-the-mill lines accumulate into <p> blocks."""
    blocks, para = [], []

    def emit_para():
        if para:
            inner = "<br>".join(_html.escape(line) for line in para)
            blocks.append("<p>" + inner + "</p>")
            para.clear()

    footer_line_re = re.compile(r"^Turn \d+ \u00b7 ")
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            emit_para()
        elif s == "---":
            emit_para()
            blocks.append("<hr>")
        elif s == COMPOSITE_IMG_MARKER:
            emit_para()
            blocks.append(COMPOSITE_IMG_MARKER)
        elif (s.startswith("---") and s.endswith("---")
              and s.strip("- ").strip()):
            emit_para()
            head = s.strip("- ").strip()
            blocks.append('<h3 class="section">' + _html.escape(head)
                          + "</h3>")
        elif s == DEATH_CLOSER:
            emit_para()
            blocks.append('<p class="closer">' + _html.escape(s) + "</p>")
        elif s.startswith("Game code:"):
            emit_para()
            foot = [s]
            while (i + 1 < len(lines)
                   and footer_line_re.match(lines[i + 1].strip())):
                i += 1
                foot.append(lines[i].strip())
            inner = "<br>".join(_html.escape(line) for line in foot)
            blocks.append('<p class="code-footer">' + inner + "</p>")
        elif not blocks and _DATELINE_RE.match(s):
            emit_para()  # first block only — never mid-narrative
            blocks.append('<p class="dateline">' + _html.escape(s)
                          + "</p>")
        else:
            para.append(s)
        i += 1
    emit_para()
    return _HTML_PAGE.format(title=_html.escape(title),
                             blocks="\n".join(blocks))


def render_turn_email(guid, view, result, player_location_slug="trailhead"):
    """Fill the §5.2 template from a completed run_turn. Returns
    (subject, text_body, html_body).

    2026-09-27 (Neil): the open prompt ("What do you do?") is GONE —
    the email ends after the world blocks, and a silent player is just
    folded into the next turn's catch-up. Death still ends with the
    closer (§5.3)."""
    guid8 = guid.replace("-", "")[:8]
    day_n, clock, tod = clock_to_day_time(result.game_clock_start)
    catchup, narrative = split_catchup(result.narrative)
    blocks = [
        f"Day {day_n} · {clock} · {tod}",
    ]
    if catchup:
        blocks += ["", catchup]
    blocks += ["", narrative, "", COMPOSITE_IMG_MARKER, "",
               "--- Known places ---",
               render_map(view, player_location_slug), "",
               f"Carrying: {render_inventory(view)}"]
    if result.game_over:
        blocks += ["", DEATH_CLOSER]
    blocks += ["", "---",
               f"Game code: {guid}",
               f"Turn {result.turn_no} · Day {day_n}, {clock}"]
    subject = f"[ATFL {guid8}] Above the Fog Line"
    text = "\n".join(blocks)
    return subject, text, body_to_html(subject, text)


def render_nudge(guid):
    """§5.3 standalone nudge (fallback only): plain system voice, ≤120
    words, never in-character, no mechanics talk. Sent as a thread
    reply; mutates nothing. Prose itself is a review checkpoint (§5) —
    this wording is placeholder structure per §2.4, not locked copy."""
    guid8 = guid.replace("-", "")[:8]
    subject = f"[ATFL {guid8}] Above the Fog Line"
    body = ("\n".join([
        "Still here — your game is waiting whenever you are.",
        "",
        "Nothing has changed in the world since your last turn; the fog",
        "is exactly where you left it. Reply to this email with what you",
        "do next, and include your Game code if you start a fresh message.",
        "",
        f"Game code: {guid}",
    ]))
    assert len(body.split()) <= 120
    return subject, body, body_to_html(subject, body)


def render_clarification(reason):
    """§5.3 clarification: fresh thread, plain and honest, never in-
    character. reason explains why the inbound couldn't be matched —
    never a guess (§1.1)."""
    subject = "[ATFL] Couldn't match your game"
    body = ("\n".join([
        "I got your message but couldn't match it to a game.",
        reason,
        "",
        "Reply with the Game code from a previous email to continue",
        "your game, or send a new email to start one.",
    ]))
    return subject, body, body_to_html(subject, body)
