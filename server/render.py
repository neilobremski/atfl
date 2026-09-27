"""Turn-email renderer — DESIGN.md §5 (renderer spec).

Fills the §5.2 block order verbatim (template lives in
prototype/turn_email_layout.txt; this module builds each block from
the filtered world view + TurnResult). text/plain only for the MVP
(§5.1). Receives the FILTERED view — hidden_traits never reaches here.

System emails (§5.3) are never in-character: clarification (fresh
thread) and the death closer live here too. The standalone-nudge
fallback (≥24h, mutates nothing, §2.3) is mailer-level: dispatch
sends the idle turn as the daily touch instead, and the nudge is only
needed if a game somehow produces no turn email in 24h — recorded as
a mailer detail, not built here.
"""
import json

OPEN_PROMPT = "What do you do?"
DEATH_CLOSER = "This was your last email. The game is over."
TIME_OF_DAY_START_MIN = 420  # DESIGN §3.1: game_clock_min=0 <=> 07:00 local


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


def render_turn_email(guid, view, result, player_location_slug="trailhead"):
    """Fill the §5.2 template from a completed run_turn. Returns
    (subject, body). Death replaces the prompt with the closer (§5.3)."""
    guid8 = guid.replace("-", "")[:8]
    day_n, clock, tod = clock_to_day_time(result.game_clock_start)
    catchup, narrative = split_catchup(result.narrative)
    prompt = DEATH_CLOSER if result.game_over else OPEN_PROMPT
    blocks = [
        f"Day {day_n} · {clock} · {tod}",
    ]
    if catchup:
        blocks += ["", catchup]
    blocks += ["", narrative, "", "--- Known places ---",
               render_map(view, player_location_slug), "",
               f"Carrying: {render_inventory(view)}", "",
               prompt, "", "---",
               f"Game code: {guid}",
               f"Turn {result.turn_no} · Day {day_n}, {clock}"]
    subject = f"[ATFL {guid8}] Above the Fog Line"
    return subject, "\n".join(blocks)


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
    return subject, body


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
    return subject, body
