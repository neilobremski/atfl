"""GameMaster interface + mock GM for the Phase 2 MVP turn loop.

DESIGN.md §2.2/§4.1: the GM reasons from the *filtered* world state
(physical only — the renderer and the mock never see hidden_traits) and
produces yes/no mutation questions plus one narrative string. The real
R4T-roster GM replaces MockGM behind this same interface; the pipeline
in turn_loop.py does not change.
"""
import json
import shutil
import subprocess

# DESIGN.md §4.3 roster: the five plot concepts from Neil's design notes.
# The pick is Neil's call; the demo defaults to Murph's lean.
PLOT_ROSTER = [
    "aliens",
    "government-project",
    "you-are-dead",
    "a-spell",
    "earth-changing",
]
DEMO_PLOT_PICK = "earth-changing"  # default until Neil calls the mystery


class GameMaster:
    """Interface the turn loop drives. All methods receive filtered state
    (no hidden_traits — see turn_loop.gather)."""

    def pick_plot(self, roster, context=None):
        """Choose ONE plot concept from the §4.3 roster at game start.
        context: {"game_guid", "turn_no", "game_clock"} envelope metadata
        (adapters only; the mock ignores it)."""
        raise NotImplementedError

    def adjudicate(self, player_input, filtered, context=None):
        """Return a list of mutation-question dicts:
        {"q", "answer" ("yes"/"no"), "rationale", "effect" or None}.
        effect: {"<etype>:<slug>": {"physical_state.<key>": new_value}}."""
        raise NotImplementedError

    def compose_narrative(self, player_input, questions, filtered, catchup,
                          context=None):
        """Compose the turn's narrative string from approved mutations,
        prefixed with the catch-up line when one is given."""
        raise NotImplementedError


class MockGM(GameMaster):
    """Stand-in for the real GM (scenario fog-line-mystery-v1 only).
    Handles drink attempts and impossible feats; everything else is a
    no-change look-around. Proves the plumbing, not the writing."""

    def pick_plot(self, roster, context=None):
        assert DEMO_PLOT_PICK in roster
        return DEMO_PLOT_PICK

    def adjudicate(self, player_input, filtered, context=None):
        bottle = filtered["objects"]["water-bottle"]
        water = json.loads(bottle["physical_state"])["water_ml"]
        text = player_input.lower()
        qs = []
        if "drink" in text:
            can = water >= 50
            qs.append({
                "q": "Can the player drink from the bottle?",
                "answer": "yes" if can else "no",
                "rationale": f"bottle holds {water} ml; a mouthful needs ~50 ml",
                "effect": {
                    "object:water-bottle": {"physical_state.water_ml": water - 150},
                    "actor:player": {"physical_state.hunger": 0.1},
                } if can else None,
            })
        if "fell" in text or "chop" in text or "tree" in text:
            fatigue = json.loads(filtered["actors"]["player"]["physical_state"])["fatigue"]
            qs.append({
                "q": "Is the player strong enough to fell a tree?",
                "answer": "no",
                "rationale": f"fatigue {fatigue}, no axe in inventory — only chips it",
                "effect": {"place:trailhead": {"physical_state.ground_wetness": "bark chips scattered"}},
            })
        if not qs:
            qs.append({"q": "Does anything change?", "answer": "no",
                       "rationale": "the player only looks around", "effect": None})
        return qs

    def compose_narrative(self, player_input, questions, filtered, catchup,
                           context=None):
        bits = []
        for qd in questions:
            if qd["answer"] == "yes" and qd["effect"]:
                targets = ", ".join(qd["effect"].keys())
                bits.append(f"{qd['q']} ({targets} changed)")
            elif qd["answer"] == "no":
                bits.append(f"{qd['q']} ({qd['rationale']})")
        body = " ".join(bits) + " Below, the fog does not move."
        return f"{catchup}\n\n{body}" if catchup else body


def _turn_failed(msg):
    # Lazy import: turn_loop imports .gm at module level, so an
    # import-time `from .turn_loop import TurnFailed` here would loop.
    from .turn_loop import TurnFailed
    raise TurnFailed(f"roster: {msg}")


# research/phase4-gm-integration.md: envelope key set. The roster must
# never see anything outside these keys (no hidden_traits, no plot,
# no ledger — enforced here, not by asking nicely).
ENVELOPE_KEYS = ("call", "game_guid", "turn_no", "game_clock",
                 "player_input", "filtered", "plot_roster", "catchup")

# Hard cap on roster narrative output (phase4 doc §: the envelope).
MAX_NARRATIVE_WORDS = 2000

# Per-call timeout, generous for a free model, bounded for the poll cycle.
ROSTER_TIMEOUT_S = 300


def game_clock_label(clock_min):
    """game_clock_min -> 'day N, ~HH:MM' (the envelope's game_clock)."""
    day = clock_min // 1440 + 1
    hh, mm = (clock_min % 1440) // 60, clock_min % 60
    return f"day {day}, ~{hh:02d}:{mm:02d}"


def build_envelope(call, game_guid, turn_no, game_clock, player_input,
                   filtered, plot_roster, catchup):
    """Build the roster call envelope. The roster gets the *filtered* view
    it was handed — this function never consults the DB, so nothing hidden
    can leak through this path by construction."""
    return {
        "call": call,
        "game_guid": game_guid,
        "turn_no": turn_no,
        "game_clock": game_clock,
        "player_input": player_input,
        "filtered": filtered,
        "plot_roster": list(plot_roster),
        "catchup": catchup or "",
    }


def _validate_questions(questions):
    """Adjudication replies must be a bare JSON list of question dicts."""
    if not isinstance(questions, list) or not questions:
        _turn_failed(
            f"roster adjudication is not a non-empty JSON list: {type(questions).__name__}")
    for qd in questions:
        if not isinstance(qd, dict):
            _turn_failed(f"roster adjudication item is not a dict: {qd!r}"[:120])
        if qd.get("answer") not in ("yes", "no"):
            _turn_failed(
                f"roster answer not yes/no: {qd.get('answer')!r}")
        if "q" not in qd or "rationale" not in qd:
            _turn_failed("roster question dict missing 'q'/'rationale'")
        effect = qd.get("effect")
        if effect is not None and not isinstance(effect, dict):
            _turn_failed(f"roster effect not a dict-or-null: {effect!r}"[:120])
        if isinstance(effect, dict):
            for target, changes in effect.items():
                etype, _, slug = target.partition(":")
                if etype not in ("place", "actor", "object") or not slug:
                    _turn_failed(
                        f"roster effect target not '<etype>:<slug>': {target!r}")
                if not isinstance(changes, dict) or not all(
                        k.startswith("physical_state.") for k in changes):
                    _turn_failed(
                        f"roster effect changes must be physical_state.<key> dicts: {changes!r}"[:160])
    return questions


class RosterGM(GameMaster):
    """Wraps the R4T `fogline-gm` roster behind the GameMaster interface.

    Two subprocess tells per turn (Option A): adjudicate returns JSON,
    compose_narrative returns prose; pick_plot is a third tell once per
    game. Every roster-side failure raises RosterTurnFailed (a
    TurnFailed), so the dispatch retry-once path covers it with no new
    machinery. Model-independent defenses (yes/no-only mutations,
    secrecy_check, ledger) stay in turn_loop.py.
    """

    def __init__(self, roster_name="fogline-gm",
                 timeout_s=ROSTER_TIMEOUT_S,
                 max_narrative_words=MAX_NARRATIVE_WORDS,
                 tell_fn=None):
        self.roster_name = roster_name
        self.timeout_s = timeout_s
        self.max_narrative_words = max_narrative_words
        # tell_fn(envelope_json) -> str: injectable for hermetic tests;
        # the real default shells out to `r4t tell <roster>`.
        self.tell_fn = tell_fn or self._tell_real

    def _tell_real(self, envelope_json):
        if shutil.which("r4t") is None:
            _turn_failed("r4t binary not found on PATH")
        try:
            proc = subprocess.run(
                ["r4t", "tell", self.roster_name, envelope_json],
                capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            _turn_failed(
                f"roster call timed out after {self.timeout_s}s")
        if proc.returncode != 0:
            _turn_failed(
                f"roster tell exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def _call(self, call, context, player_input="", questions=None,
              filtered=None, catchup=""):
        ctx = context or {}
        envelope = build_envelope(
            call, ctx.get("game_guid", ""), ctx.get("turn_no", 0),
            ctx.get("game_clock", ""), player_input,
            filtered or {}, PLOT_ROSTER, catchup)
        try:
            return self.tell_fn(json.dumps(envelope))
        except Exception as e:
            # _turn_failed raises TurnFailed; the `from` chain only runs
            # when it is actually a TurnFailed (lazy import, no cycle).
            _turn_failed(f"tell failed: {type(e).__name__}: {e}")

    def pick_plot(self, roster, context=None):
        reply = self._call("pick_plot", context)
        pick = reply.strip()
        if pick not in roster:
            _turn_failed(
                f"roster plot pick {pick!r} not on the §4.3 roster")
        return pick

    def adjudicate(self, player_input, filtered, context=None):
        reply = self._call("adjudicate", context, player_input=player_input,
                           filtered=filtered)
        try:
            questions = json.loads(reply)
        except json.JSONDecodeError as e:
            _turn_failed(
                f"roster adjudication is not JSON: {e}; reply: {reply[:120]!r}")
        return _validate_questions(questions)

    def compose_narrative(self, player_input, questions, filtered, catchup,
                          context=None):
        narrative = self._call("compose_narrative", context,
                               player_input=player_input,
                               questions=questions, filtered=filtered,
                               catchup=catchup)
        words = len(narrative.split())
        if words > self.max_narrative_words:
            _turn_failed(
                f"roster narrative {words} words exceeds cap {self.max_narrative_words}")
        return narrative
