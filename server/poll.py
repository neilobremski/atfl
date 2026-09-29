"""Poll loop entry point — Above the Fog Line server (Phase 2 MVP).

Runs on free-micro-1 under systemd (deploy/atfl.service). One process,
one loop: every ATFL_POLL_MIN minutes it runs a full mailer cycle
(poll -> dispatch -> send -> nudge sweep). Crashes inside a cycle are
logged and the loop continues; process-level death is systemd's job
(Restart=always). No network call happens until a real token exists —
gmail_adapter.build_service raises first.

Usage on the VM:
    /srv/atfl/venv/bin/python -m server.poll          # loop forever
    /srv/atfl/venv/bin/python -m server.poll --once   # one cycle, then exit
    /srv/atfl/venv/bin/python -m server.poll --fake   # smoke test with
                         FakeGmail + MockGM; never touches the network.
"""
import argparse
import logging
import os
import signal
import sys
import time
import traceback

from . import config
from .gmail_adapter import GoogleApiGmail, build_service, messages_resource
from .gm import MockGM, RosterGM
from .mailer import run_poll_cycle

log = logging.getLogger("atfl.poll")

_stop = False


def _handle_term(signum, _frame):
    global _stop
    log.info("signal %s received; finishing current cycle, then exiting",
             signum)
    _stop = True


def run_once(gmail, gm, cfg):
    """One poll cycle; returns the result dict. Exceptions propagate to
    the caller — the loop logs them per-cycle and keeps going."""
    started = time.time()
    result = run_poll_cycle(
        games_dir=cfg["games_dir"],
        gmail=gmail,
        gm=gm,
        game_address=cfg["game_address"],
        turn_len_min=cfg["turn_len_min"],
        images={"mode": cfg["images_mode"], "api_key": cfg["image_api_key"]},
    )
    sent = result.get("sent", [])
    nudged = result.get("nudged", [])
    log.info("cycle done in %.1fs: %d inbound processed, %d sent, %d nudged",
             time.time() - started, len(sent), len(sent), len(nudged))
    for s in sent:
        log.info("sent: %s action=%s guid=%s turn=%s note=%s",
                 s.get("sender"), s.get("action"), s.get("guid"),
                 s.get("turn_no"), s.get("note"))
    return result


def build_clients(cfg):
    """gm per ATFL_GM (mock default; RosterGM for 'roster') and the Gmail
    client (real adapter once the token exists). Called once at startup."""
    gm = (RosterGM(node_name=cfg["a8s_node"],
                   node_root=cfg["a8s_node_root"])
          if cfg["gm"] == "roster" else MockGM())
    service = build_service(cfg["token_path"])  # raises until OQ#1 closes
    gmail = GoogleApiGmail(messages_resource(service))
    return gmail, gm


def main(argv=None):
    ap = argparse.ArgumentParser(description="Above the Fog Line poll loop")
    ap.add_argument("--once", action="store_true",
                    help="run one poll cycle, then exit")
    ap.add_argument("--fake", action="store_true",
                    help="smoke test: FakeGmail + MockGM, no network")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    try:
        cfg = config.load()
    except config.ConfigError as e:
        log.error("startup refused: %s", e)
        return 2

    os.makedirs(cfg["games_dir"], exist_ok=True)
    log.info("games dir: %s", cfg["games_dir"])

    if args.fake:
        from .mailer import FakeGmail
        gmail = FakeGmail(game_address=cfg["game_address"])
        gm = MockGM()
        log.info("FAKE MODE: no network, no token needed")
    else:
        try:
            gmail, gm = build_clients(cfg)
        except RuntimeError as e:
            log.error("cannot build Gmail client: %s", e)
            return 2
        log.info("polling %s every %d min (turn length %d min)",
                 cfg["game_address"], cfg["poll_min"], cfg["turn_len_min"])

    run_once(gmail, gm, cfg)
    if args.once or args.fake:
        return 0

    while not _stop:
        # Sleep in short slices so SIGTERM lands quickly even with a
        # long cadence (exit after the current cycle finishes).
        deadline = time.time() + cfg["poll_min"] * 60
        while time.time() < deadline and not _stop:
            time.sleep(min(5, deadline - time.time()))
        if _stop:
            break
        try:
            run_once(gmail, gm, cfg)
        except Exception:
            log.error("cycle failed; loop continues\n%s",
                      traceback.format_exc())
    log.info("poll loop stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
