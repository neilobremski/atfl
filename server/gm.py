"""GameMaster interface + mock GM for the Phase 2 MVP turn loop.

DESIGN.md §2.2/§4.1: the GM reasons from the *filtered* world state
(physical only — the renderer and the mock never see hidden_traits) and
produces yes/no mutation questions plus one narrative string. The real
R4T-roster GM replaces MockGM behind this same interface; the pipeline
in turn_loop.py does not change.
"""
import json
import os
import shutil
import subprocess
import time

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


def _a8s_bin():
    """Locate the a8s binary: explicit ATFL_A8S_BIN env, then PATH, then
    the operator's ~/.ar3/a8s (this box's install, not on PATH in worker
    shells — found live 2026-09-28 when the bare lookup would have
    TurnFailed'd a real RosterGM call). Loud failure when none exists."""
    explicit = os.environ.get("ATFL_A8S_BIN")
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        return explicit
    on_path = shutil.which("a8s")
    if on_path:
        return on_path
    home_bin = os.path.expanduser("~/.ar3/a8s")
    if os.path.isfile(home_bin) and os.access(home_bin, os.X_OK):
        return home_bin
    _turn_failed("a8s binary not found — set ATFL_A8S_BIN or put a8s on PATH")


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


def _outbox_files(outbox_dir):
    """Filenames of unreceipted outbox files (top-level *.json only —
    .receipts/ holds published ones). Missing dir reads as empty."""
    try:
        return {f for f in os.listdir(outbox_dir) if f.endswith(".json")}
    except OSError:
        return set()


# research/phase4-gm-integration.md: envelope key set. The roster must
# never see anything outside these keys (no hidden_traits, no plot,
# no ledger — enforced here, not by asking nicely).
# "adjudication" rides only on compose_narrative calls (correction 4 in
# the live two-tell exercise: external tells open fresh a8s threads, so
# keeper can't see the earlier adjudication tell).
ENVELOPE_KEYS = ("call", "game_guid", "turn_no", "game_clock",
                 "player_input", "filtered", "plot_roster", "catchup",
                 "adjudication")

# Hard cap on roster narrative output (phase4 doc §: the envelope).
MAX_NARRATIVE_WORDS = 2000

# Reply wait: the a8s transport is async, so a subprocess call budget is
# meaningless here — keeper answer latency is tens-of-minutes scale.
# Measured 2026-09-30 across three live pick_plot attempts: first keeper
# replies landed 34, 50, and 69 min after the send (a fourth stale answer
# at 143 min); all above the old 2100s total budget, so turns failed on
# latency alone. The game plays at roughly an email a day, so a 2.5h
# per-attempt budget is invisible to players; the poll cycle can
# send-and-return and pick the reply up on a later cycle, and RosterGM's
# blocking default covers the simpler case.
REPLY_WAIT_S = 7200
POLL_INTERVAL_S = 30

# Re-prompt budget: after REPLY_WAIT_S elapses with no keeper reply, the
# server sends ONE reprompt envelope (2026-09-29: an operator re-prompt
# recovered the starved adjudicate return leg live — never reconstruct,
# forward the already-produced artifact) and waits this long before
# failing loudly via TurnFailed. None/0 disables the re-prompt.
REPROMPT_WAIT_S = 1800

# Publish wait: after `a8s tell` records the outbox file, _send_real waits
# this long for the node's daemon to publish it (file drained from the
# outbox dir into .receipts) BEFORE running `a8s stop`. `a8s tell` only
# *records* the file — the S3 publish is the daemon's asynchronous job —
# so stopping the daemon first strands the tell silently in the outbox
# (found live 2026-09-30: the stop's SIGTERM beat the daemon's first poll
# tick by ~350ms; the pick_plot never published and the full 2h reply
# wait burned on a call the keeper never received). The drain, not the
# start's exit code, is the proof of publish.
PUBLISH_WAIT_S = 120

# Sender attribution on keeper's replies (correction 3: keeper replies to
# the inbound sender, so production sends come from the game server's own
# mailbox-only a8s node, e.g. atfl-server, never-started).
KEEPER_SENDER = "fogline-gm:keeper"


# The re-prompt is a new call type with its own key set (not a subset of
# ENVELOPE_KEYS): it embeds the ORIGINAL envelope verbatim so keeper can
# match the open call and reply with the already-produced artifact. The
# original envelope is already filtered (build_envelope never consults
# the DB), so embedding it here cannot leak hidden state.
REPROMPT_KEYS = ("call", "game_guid", "turn_no", "game_clock",
                 "original_call", "original_envelope")


def build_reprompt_envelope(original_envelope):
    """One nudge after a silent reply wait. The roster must reply with the
    already-produced artifact for the original call (or finish it from
    the original envelope) — never redo the work, never re-consult."""
    return {
        "call": "reprompt",
        "game_guid": original_envelope["game_guid"],
        "turn_no": original_envelope["turn_no"],
        "game_clock": original_envelope["game_clock"],
        "original_call": original_envelope["call"],
        "original_envelope": original_envelope,
    }


def game_clock_label(clock_min):
    """game_clock_min -> 'day N, ~HH:MM' (the envelope's game_clock)."""
    day = clock_min // 1440 + 1
    hh, mm = (clock_min % 1440) // 60, clock_min % 60
    return f"day {day}, ~{hh:02d}:{mm:02d}"


def build_envelope(call, game_guid, turn_no, game_clock, player_input,
                   filtered, plot_roster, catchup, adjudication=None):
    """Build the roster call envelope. The roster gets the *filtered* view
    it was handed — this function never consults the DB, so nothing hidden
    can leak through this path by construction. adjudication is the
    validated question array from the earlier adjudicate call; only
    compose_narrative sets it (ENVELOPE_KEYS pins the full key set)."""
    return {
        "call": call,
        "game_guid": game_guid,
        "turn_no": turn_no,
        "game_clock": game_clock,
        "player_input": player_input,
        "filtered": filtered,
        "plot_roster": list(plot_roster),
        "catchup": catchup or "",
        "adjudication": list(adjudication or []),
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

    Async transport (correction 1 of the live two-tell exercise): `a8s tell`
    queues and returns — nothing comes back on stdout. So each call is
    send (`a8s tell fogline-gm '<envelope>'`) then wait on the game
    server's own a8s mailbox for keeper's reply (`from fogline-gm:keeper`,
    correlated by send time since the server runs serial turns).

    Production shape: the game server's tells go out from its own
    mailbox-only a8s node (default `atfl-server`; `node_name` + `node_root`
    come from config — see ATFL_A8S_NODE / ATFL_A8S_NODE_ROOT). `node_root`
    is that node's root directory (the cwd `a8s tell` must run from —
    keeper replies to the inbound sender, so a tell sent from any other
    node strands the reply).

    Start-send-stop is enforced in code, not left to operator procedure
    (the publish mechanism was found live 2026-09-29): `_send_real`
    starts the node (a never-started node's `tell` only records the
    outbox file — the S3 publish is the running daemon's job), sends,
    then stops the node in a finally before the reply poll (a running
    daemon consumes inbound before `a8s convo` sees it). A missing or
    non-directory `node_root` fails loudly in `_send_real` before any
    subprocess runs — never silently sends from the caller's cwd.

    When the reply wait exhausts with no keeper reply, one `reprompt`
    envelope goes out (the original envelope verbatim, with
    original_call + REPROMPT_KEYS) and the wait re-arms for
    reprompt_wait_s — the live-validated operator recovery pattern,
    automated. A late reply to the original call still counts (the poll
    correlates everything newer than the original send). Only after the
    re-prompt also times out does the call fail loudly.

    Every roster-side failure raises RosterTurnFailed (a TurnFailed), so
    the dispatch retry-once path covers it with no new machinery.
    Model-independent defenses (yes/no-only mutations, secrecy_check,
    ledger) stay in turn_loop.py.
    """

    def __init__(self, roster_name="fogline-gm",
                 reply_wait_s=REPLY_WAIT_S,
                 poll_interval_s=POLL_INTERVAL_S,
                 reprompt_wait_s=REPROMPT_WAIT_S,
                 publish_wait_s=PUBLISH_WAIT_S,
                 max_narrative_words=MAX_NARRATIVE_WORDS,
                 node_name="atfl-server",
                 node_root=None,
                 keeper_sender=KEEPER_SENDER,
                 send_fn=None, poll_fn=None):
        self.roster_name = roster_name
        self.reply_wait_s = reply_wait_s
        self.poll_interval_s = poll_interval_s
        self.reprompt_wait_s = reprompt_wait_s
        self.publish_wait_s = publish_wait_s
        self.max_narrative_words = max_narrative_words
        self.node_name = node_name
        self.node_root = node_root
        self.keeper_sender = keeper_sender
        # send_fn(envelope_json) -> None; poll_fn(timeout_s, since_iso)
        # -> first unseen keeper reply body (str) or None. Injectable for
        # hermetic tests; the real defaults shell out to `a8s`.
        self.send_fn = send_fn or self._send_real
        self.poll_fn = poll_fn or self._poll_real

    def _send_real(self, envelope_json):
        """Publish the envelope to the roster via the game server's own
        mailbox node, start-send-stop (found live 2026-09-29): `a8s tell`
        only *records* the outbox file — the S3 publish is done by the
        node's running daemon, so the node is started first; it is
        stopped again in a finally before the reply poll, because a
        running daemon consumes inbound before `a8s convo` sees it.
        Outbound MUST go from node_root (keeper replies to the inbound
        sender) — a missing node_root fails loudly here, before any
        subprocess runs, instead of silently sending from the caller's
        cwd and stranding the reply.
        TELL_OUTBOX_DIR guard (found live 2026-09-30): `a8s tell`
        prefers the TELL_OUTBOX_DIR env var over the cwd-based registry
        lookup, so a stray export (a documented workspace convention for
        bare-shell sends) silently routed roster calls through the wrong
        node's outbox; the ingesting daemon force-overwrote `from`, the
        keeper replied to the wrong mailbox, and the reply poll starved
        for two full attempts while the keeper was actually answering.
        The var is scrubbed from the child env and the resolved outbox
        is verified against node_root before anything is sent."""
        if not self.node_root or not os.path.isdir(self.node_root):
            _turn_failed(
                "RosterGM node_root is not set — register the game "
                "server's mailbox-only a8s node (e.g. atfl-server) and "
                "point RosterGM at its root directory")
        a8s = _a8s_bin()
        node_root = os.path.realpath(self.node_root)
        # Scrub TELL_OUTBOX_DIR: it overrides the cwd-based outbox
        # resolution and would silently attribute the send to whatever
        # node owns that outbox (found live 2026-09-30).
        child_env = {k: v for k, v in os.environ.items()
                     if k != "TELL_OUTBOX_DIR"}
        try:
            check = subprocess.run(
                [a8s, "tell", "--check", self.roster_name],
                capture_output=True, text=True, timeout=60,
                cwd=self.node_root, env=child_env)
        except subprocess.TimeoutExpired:
            _turn_failed("a8s tell --check did not return within 60s")
        if check.returncode != 0:
            _turn_failed(
                f"a8s tell --check {self.roster_name} exited "
                f"{check.returncode}: {check.stderr.strip()[:200]}")
        outbox = None
        for line in check.stdout.splitlines():
            if line.startswith("  outbox: "):
                outbox = os.path.realpath(line[len("  outbox: "):].strip())
                break
        if not outbox or os.path.commonpath([outbox, node_root]) != node_root:
            _turn_failed(
                f"a8s tell would send from outbox {outbox!r}, outside "
                f"node_root {node_root!r} — refusing to strand the keeper "
                "reply in the wrong mailbox")
        outbox_dir = os.path.join(node_root, ".outbox")
        pending_before = _outbox_files(outbox_dir)
        try:
            start = subprocess.run(
                [a8s, "start", self.node_name],
                capture_output=True, text=True, timeout=60,
                env=child_env)
            if start.returncode != 0:
                _turn_failed(f"a8s start {self.node_name} exited "
                             f"{start.returncode}: "
                             f"{start.stderr.strip()[:200]}")
            published = False
            try:
                proc = subprocess.run(
                    [a8s, "tell", self.roster_name, envelope_json],
                    capture_output=True, text=True, timeout=60,
                    cwd=self.node_root, env=child_env)
                if proc.returncode != 0:
                    _turn_failed(
                        f"a8s tell exited {proc.returncode}: "
                        f"{proc.stderr.strip()[:200]}")
                # The daemon publishes asynchronously: `a8s tell` only
                # *records* the outbox file, the S3 publish is the
                # daemon's job. Wait for the drain BEFORE stopping the
                # node — stopping first strands the tell silently in
                # the outbox (found live 2026-09-30: the stop's SIGTERM
                # beat the daemon's first poll tick by ~350ms, the
                # pick_plot never published, and the full reply wait
                # burned on a call the keeper never received). The
                # drain — not `a8s start` exiting 0 — is the proof the
                # keeper can see the call.
                self._await_publish(outbox_dir, pending_before)
                published = True
            finally:
                # Stop failure is loud, not best-effort: a daemon left
                # running races the reply poll and would silently starve
                # the call (the whole reply_wait_s + re-prompt burn).
                # Exception: when the publish never confirmed, the call
                # has already failed loudly above — then stop runs
                # best-effort so a dead ("not running") daemon can't
                # mask the stranded-publish error with a misleading
                # stop error.
                try:
                    stop = subprocess.run(
                        [a8s, "stop", self.node_name],
                        capture_output=True, text=True, timeout=660,
                        env=child_env)
                except subprocess.TimeoutExpired:
                    if published:
                        _turn_failed(
                            f"a8s stop {self.node_name} did not return "
                            f"within 660s — the reply poll would race a "
                            f"live daemon")
                else:
                    if stop.returncode != 0 and published:
                        _turn_failed(
                            f"a8s stop {self.node_name} exited "
                            f"{stop.returncode}: "
                            f"{stop.stderr.strip()[:200]} — the reply poll "
                            f"would race a live daemon")
        except subprocess.TimeoutExpired:
            _turn_failed("a8s send sequence did not return within 60s")

    def _await_publish(self, outbox_dir, pending_before):
        """Block until the daemon publishes every outbox file recorded
        after pending_before was snapshotted (the tell just sent drains
        from the outbox dir into .receipts), or fail loudly after
        publish_wait_s. No stop is attempted on timeout: the daemon is
        dead or wedged, and the message names the stranded file so the
        node can be investigated."""
        deadline = time.monotonic() + self.publish_wait_s
        while True:
            pending = _outbox_files(outbox_dir) - pending_before
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _turn_failed(
                    f"roster tell recorded but not published within "
                    f"{self.publish_wait_s}s (stranded in outbox: "
                    f"{sorted(pending)[0][:48]}...) — the node daemon "
                    f"may be dead; not stopping it, investigate the node")
            time.sleep(min(1.0, remaining))

    def _poll_real(self, timeout_s, since_iso):
        """One poll arm on the game server's mailbox node; returns the first
        keeper reply newer than since_iso, or None.

        Uses `a8s convo <node_name> --from <keeper> --json` — the game
        server's OWN mailbox node, because keeper replies to the inbound
        sender (the node that sent the call). NOT `convo <roster_name>`:
        the roster thread only shows our outbound calls, so polling it
        returns zero rows forever while keeper replies pile up unheard
        in the node mailbox (live-caught 2026-09-29: six "earth-changing"
        replies sat in atfl-server's mailbox while every poll arm read
        the fogline-gm thread and timed out).

        Verified working live 2026-09-29 — the documented `tells --from`/
        `--json`/`--since` flags are all rejected or broken in a8s 0.1.97;
        see _send_real's publish note and the ares bug report. One arm is
        a fast history read; `_call` re-arms every poll_interval_s until
        the reply wait deadline. Python-side sender + timestamp filtering
        is the real correlation — `--from` is belt only."""
        if not self.node_root or not os.path.isdir(self.node_root):
            _turn_failed(
                "RosterGM node_root is not set — register the game "
                "server's mailbox-only a8s node (e.g. atfl-server) and "
                "point RosterGM at its root directory")
        proc = subprocess.run(
            [_a8s_bin(), "convo", self.node_name,
             "--from", self.keeper_sender, "--json", "--limit", "25"],
            capture_output=True, text=True, timeout=timeout_s + 30,
            cwd=self.node_root)
        if proc.returncode != 0:
            _turn_failed(f"a8s convo exited {proc.returncode}: "
                         f"{proc.stderr.strip()[:200]}")
        return self._parse_tells(proc.stdout, since_iso)

    def _parse_tells(self, output, since_iso):
        """Pull the first keeper reply body out of `a8s convo --json`:
        newline-delimited {ulid, seq, from, to, utc, content, ...} rows.
        Accepts the first row sent by keeper *newer* than since_iso (both
        UTC ISO, zero-padded — string comparison is chronological). None
        when nothing arrived. Non-JSON lines are ignored."""
        want = self.keeper_sender.lower()
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("from", "")).lower() != want:
                continue
            if str(row.get("utc", "")) <= since_iso:
                continue
            body = (row.get("content") or "").strip()
            if body:
                return body
        return None

    def _send(self, payload):
        from .turn_loop import TurnFailed  # lazy: turn_loop imports .gm
        try:
            self.send_fn(payload)
        except TurnFailed:
            raise
        except Exception as e:
            _turn_failed(f"roster send failed: {type(e).__name__}: {e}")

    def _wait(self, sent_at, budget_s):
        """Poll until a keeper reply newer than sent_at arrives, or the
        budget exhausts. Returns the reply body, or None on timeout
        (timeout is a soft signal here — the caller decides whether to
        re-prompt or fail loudly)."""
        from .turn_loop import TurnFailed  # lazy: turn_loop imports .gm
        deadline = time.monotonic() + budget_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                body = self.poll_fn(min(self.poll_interval_s, remaining),
                                    sent_at)
            except TurnFailed:
                raise
            except Exception as e:
                _turn_failed(
                    f"roster mailbox poll failed: {type(e).__name__}: {e}")
            if body:
                return body

    def _call(self, call, context, player_input="", questions=None,
              filtered=None, catchup=""):
        ctx = context or {}
        envelope = build_envelope(
            call, ctx.get("game_guid", ""), ctx.get("turn_no", 0),
            ctx.get("game_clock", ""), player_input,
            filtered or {}, PLOT_ROSTER, catchup,
            adjudication=questions if call == "compose_narrative" else None)
        self._send(json.dumps(envelope))
        sent_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        body = self._wait(sent_at, self.reply_wait_s)
        if body is None and self.reprompt_wait_s:
            # One bounded re-prompt before the loud failure: the
            # starved return leg may already exist on the roster side
            # (session #37 — the artifact was produced but never sent).
            reprompt = build_reprompt_envelope(envelope)
            self._send(json.dumps(reprompt))
            # Keep the ORIGINAL sent_at: a late reply to the original
            # call arriving during the re-prompt wait is the reply.
            body = self._wait(sent_at, self.reprompt_wait_s)
        if body is None:
            budget = (f"{self.reply_wait_s}s"
                      + (f" + {self.reprompt_wait_s}s re-prompt"
                         if self.reprompt_wait_s else ""))
            _turn_failed(f"no roster reply within {budget} for {call}")
        return body

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
