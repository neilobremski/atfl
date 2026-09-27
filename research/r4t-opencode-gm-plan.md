# R4T GM engine plan — OpenCode (free) roster of one with memory

*2026-09-27 — the concrete first step Neil assigned for OQ#2 (R4T GM model):
"create an engine running OpenCode (free), a roster of one in R4T with memory,
using their built-in models — a good test of R4T."*

## What this doc is

The mapped-out plan from session #19's R4T investigation (AR3 docs read:
`r4t.md`, `r4t-engine.md`, `r4t-tutorial.md`, `r4t-engine-memory.md`,
guide chapter 2; `~/.ar3` is the AR3 checkout — `r4t` runs from
`~/.ar3/r4t`, not installed on PATH). One install was done live (opencode
binary, no account needed); the rest waits on Neil's one-time sign-in.
Setup session steps are copy-paste ready in §5.

## What I verified this session (on this VM)

- **opencode 1.18.32** installs fine under HTTPS-only egress; binary lives at
  `~/.opencode/bin/opencode` (not on PATH — must be added for any a8s wake).
- `r4t engine list` → `opencode [quota, run, check]`, preset `opencode`:
  `opencode run --auto --dir {workdir} {prompt}`, continue supported via
  `--continue` (roster `- **Continue:** on` — per-directory conversation
  store, graded **yes** for roster continuation).
- `r4t engine opencode check` → `accepted (help scan)`, with and without
  `--model "opencode/deepseek-v4-flash-free"`. No turn spent, no auth needed
  for check.
- **Zen free models are real** (Sept 2026, community catalogs; roster churns
  — recheck `curl https://opencode.ai/zen/v1/models` before pinning):
  `opencode/deepseek-v4-flash-free` (tools ✅), `opencode/big-pickle`
  (no tools), `opencode/nemotron-3-ultra-free`, `opencode/qwen3.6-plus-free`,
  `opencode/mimo-v2.5-free`, `opencode/minimax-m2.5-free`. CLI usage:
  `opencode run --model "opencode/deepseek-v4-flash-free" "…"`.
- **Memory**: roster member gets a private k7e store via the runbook line
  `- **Knowledge:** on` (bytes budget, not tokens; default tier for
  opencode-class rigs is the lower tier, `r4t roster check` warns but does
  not refuse). Every turn: keyword(+optional local embeddings) retrieval
  packed into the prompt, post-turn capture, bounded background worker
  distills learnings. Corrections retire entries asynchronously — for the GM
  this means Neil's corrections in email can correct the GM's stored facts.
- **Engine `run` vs roster**: bare `r4t engine opencode run` is stateless
  (scaffold reads STATUS.md/LESSONS.md); the roster path is what Neil asked
  for — governance, budgets, queue, continuation.

## Roster design (roster of one, name: `fogline-gm`)

Node dir: `~/ar3/fogline-gm/` (not inside the game repo — the runbook is
repo state for *this* roster; the game repo stays the GM's workplace via
frontmatter `workdir:`). One file, `r4t.md`:

```markdown
---
name: "fogline-gm"
extends: "triforce"
---

# fogline-gm

## Mission

Run the Above the Fog Line game master: one governed turn per player email,
advancing the mystery per the design doc. Stay in character as the GM; never
invent game facts the design rules don't allow.

## Roster

### gm
- **Rig:** gm
- **Leader:** yes
- **Continue:** on
- **Knowledge:** on
- **Workdir:** /home/hatch/workspace/above-the-fog-line
- **Role:** The game master — reads the player email, advances the game per
  DESIGN.md, writes the turn email text, and replies.

## Rituals

None yet.
```

(Empty `## Rituals` replaces triforce's — its standup/mission-review
rituals address `Lead`, who doesn't exist here; leaving the section out is a
load error, per guide ch. 2 §4.)

Rig (machine config, outside any repo — `~/.config/r4t/rigs.json`):

```bash
r4t rig add gm opencode --model opencode/deepseek-v4-flash-free
r4t rig set gm echo true     # stdout-only replies: roster of one talks to us
```

Model pick rationale: deepseek-v4-flash-free supports **tools** (big-pickle
does not) — the GM needs file tools (SQLite game DB, game code). If the free
roster churns, `opencode/qwen3.6-plus-free` and `opencode/mimo-v2.5-free`
are tool-capable alternates.

Registration (after auth exists):

```bash
export PATH="$HOME/.opencode/bin:$PATH"
r4t runbook check
r4t add ~/ar3/fogline-gm        # binds name fogline-gm = leader = gm
tell fogline-gm "Introduce yourself as the GM."
```

Permission ceiling: `r4t add` records `ceiling: permissions auto` — opencode's
own strongest mode is `auto`, so nothing to raise. Keep `--permissions` at
the preset default (fail-closed on claude-class rigs; opencode runs
fail-open `auto` by preset, so the roster's budget + queue are the real
governors).

## Why this fits the game

One GM turn ≈ one game turn: `- **Continue:** on` keeps the GM's conversation
across the month of play (with the 15m-idle-retire pattern from guide ch. 2
§11 if stale context becomes a problem), the k7e `Knowledge:` store holds the
GM's durable facts (plot concept, discovered places — distinct from the
game's SQLite truth, which stays the real state), and r4t's budget bucket
caps spend per hour. Free Zen models make the month-long GM turn cadence cost
$0/token.

## What Neil must do (one-time, can't be delegated)

1. **OpenCode account + sign-in.** `opencode auth login` is interactive OAuth
   (Google/GitHub). He can also hand over a Zen API key
   (https://opencode.ai/auth) instead — I can wire it into the rig's env.
   Two caveats I found, stated plainly:
   - One community source says Zen still requires a **$20 one-time top-up**
     to issue a key ("free" = $0/token, not $0 signup); another says no card
     needed. He should confirm at sign-up — if it needs a top-up, that's his
     spending call.
   - Free Zen models **may use session data for model improvement**. Fine
     for a game GM, but he should know before any personal content flows
     through it.
2. Nothing else — install, runbook draft, rig wiring are all on me.

## Sequencing with the rest of the build

- The R4T GM is the eventual Phase 2/4 GM backend; the MVP's mock-GM
  (`server/gm.py` mock) keeps dev unblocked until this roster is live.
- After the roster answers its first tells, wire the game's poll cycle to
  `r4t engine opencode run --agent fogline-gm …` (or `r4t tell`) as the
  GM-adjudication call replacing the mock — Phase 4 work, not this session.
- OpenCode binary install is done; consider adding `~/.opencode/bin` to the
  VM's persistent PATH for future sessions (PATH note in TOOLS.md).
