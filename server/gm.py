"""GameMaster interface + mock GM for the Phase 2 MVP turn loop.

DESIGN.md §2.2/§4.1: the GM reasons from the *filtered* world state
(physical only — the renderer and the mock never see hidden_traits) and
produces yes/no mutation questions plus one narrative string. The real
R4T-roster GM replaces MockGM behind this same interface; the pipeline
in turn_loop.py does not change.
"""
import json

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

    def pick_plot(self, roster):
        """Choose ONE plot concept from the §4.3 roster at game start."""
        raise NotImplementedError

    def adjudicate(self, player_input, filtered):
        """Return a list of mutation-question dicts:
        {"q", "answer" ("yes"/"no"), "rationale", "effect" or None}.
        effect: {"<etype>:<slug>": {"physical_state.<key>": new_value}}."""
        raise NotImplementedError

    def compose_narrative(self, player_input, questions, filtered, catchup):
        """Compose the turn's narrative string from approved mutations,
        prefixed with the catch-up line when one is given."""
        raise NotImplementedError


class MockGM(GameMaster):
    """Stand-in for the real GM (scenario fog-line-mystery-v1 only).
    Handles drink attempts and impossible feats; everything else is a
    no-change look-around. Proves the plumbing, not the writing."""

    def pick_plot(self, roster):
        assert DEMO_PLOT_PICK in roster
        return DEMO_PLOT_PICK

    def adjudicate(self, player_input, filtered):
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

    def compose_narrative(self, player_input, questions, filtered, catchup):
        bits = []
        for qd in questions:
            if qd["answer"] == "yes" and qd["effect"]:
                targets = ", ".join(qd["effect"].keys())
                bits.append(f"{qd['q']} ({targets} changed)")
            elif qd["answer"] == "no":
                bits.append(f"{qd['q']} ({qd['rationale']})")
        body = " ".join(bits) + " Below, the fog does not move."
        return f"{catchup}\n\n{body}" if catchup else body
