# Retired demos

Demos whose subject was deliberately removed from the codebase —
kept for history, not run.

- `gmail_adapter_demo.py` — retired 2026-10-07 (work session #140):
  `server/gmail_adapter.py` was deleted in the relay-edition rewrite
  (f2ed606, 2026-10-02; the engine no longer touches Gmail — Murph is
  the exchange layer on the Inkbox). The demo imported the deleted
  module, so it can never go green again.
