#!/usr/bin/env python3
"""Bridge: a8s daemon wake -> engine poll_inbound inbox (free-micro-1).

The atfl-server node's definition (deploy/atfl-server-definition.json)
invokes this with the received tell's body as argv[1]. Murph's forwarder
(hidden_files/atfl_relay_forwarder.py) sends the atfl_inbound envelope as
the raw body, so we write it verbatim to the engine's inbox dir
(<ATFL_A8S_AGENTS_DIR>/atfl-server/inbox/), which server/murph_relay.py
poll_inbound() scans. Bodies that are not atfl_inbound envelopes go to
dropped/ for inspection instead of the inbox, so poll_inbound only ever
sees files it can consume.

Why this exists: the a8s daemon delivers to an attached node by waking
its definition's invoke command; it does NOT leave files in the agents
inbox dir for poll_inbound to scan. This script is the last hop of the
inbound leg (player -> Murph -> engine).

Atomic write (tmp + os.replace). The filename is deterministic from
inkbox_message_id (the engine's dedupe key), so a duplicate wake
overwrites the same file instead of creating a second one.
"""

import json
import os
import sys
import time

# Must match ATFL_A8S_AGENTS_DIR in /etc/atfl/atfl.env.
AGENTS_DIR = "/srv/atfl/a8s/engine-inbox"
NODE = "atfl-server"

INBOX = os.path.join(AGENTS_DIR, NODE, "inbox")
DROPPED = os.path.join(AGENTS_DIR, NODE, "dropped")


def main():
    body = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        env = json.loads(body)
        ok = (
            isinstance(env, dict)
            and env.get("kind") == "atfl_inbound"
            and env.get("inkbox_message_id")
        )
    except (ValueError, TypeError):
        ok = False
    if ok:
        mid = str(env["inkbox_message_id"]).replace("/", "_")
        dest_dir, name = INBOX, "inbound-%s.json" % mid
    else:
        dest_dir = DROPPED
        name = "raw-%d-%d.json" % (int(time.time()), os.getpid())
    os.makedirs(dest_dir, exist_ok=True)
    tmp = os.path.join(dest_dir, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, os.path.join(dest_dir, name))
    print(
        "atfl-server inbox_append: %s -> %s (%d bytes)"
        % ("kept" if ok else "dropped", os.path.join(dest_dir, name), len(body))
    )


if __name__ == "__main__":
    main()
