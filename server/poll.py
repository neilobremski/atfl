"""Poll loop entry point — Above the Fog Line server (Phase 2 MVP).

Runs on free-micro-1 under systemd (deploy/atfl.service). One process,
one loop: every ATFL_POLL_MIN minutes it runs a full relay cycle
(poll A8S inbox -> dispatch -> hand off to Murph -> nudge sweep).
Crashes inside a cycle are logged and the loop continues; process-level
death is systemd's job (Restart=always). Outbound leaves via `a8s tell`
to ATFL_MURPH_NODE — a failed handoff is loud (§2.6), never half-sent.

Usage on the VM:
    /srv/atfl/venv/bin/python -m server.poll          # loop forever
    /srv/atfl/venv/bin/python -m server.poll --once   # one cycle, then exit
    /srv/atfl/venv/bin/python -m server.poll --fake   # smoke test with
                         FakeGmail + MockGM; shells no `a8s tell`.
"""
import argparse
import logging
import os
import signal
import sys
import time
import traceback

from . import config
from .gm import MockGM, RosterGM
from .mailer import FakeGmail, run_poll_cycle

log = logging.getLogger("atfl.poll")

_stop = False


def _handle_term(signum, _frame):
    global _stop
    log.info("signal %s received; finishing current cycle, then exiting",
             signum)
    _stop = True


def run_once(relay, gm, cfg):
    """One poll cycle; returns the result dict. Exceptions propagate to
    the caller — the loop logs them per-cycle and keeps going."""
    started = time.time()
    kwargs = dict(
        games_dir=cfg["games_dir"],
        gm=gm,
        engine_node=cfg["a8s_node"],
        murph_node=cfg["murph_node"],
        node_root=cfg["a8s_node_root"],
        turn_len_min=cfg["turn_len_min"],
        images={"mode": cfg["images_mode"],
                "api_key": cfg["image_api_key"],
                "hf_token": cfg["hf_token"]},
    )
    if relay is not None:
        kwargs["relay"] = relay
    result = run_poll_cycle(**kwargs)
    sent = result.get("sent", [])
    nudged = result.get("nudged", [])
    handed = sum(1 for s in sent if s.get("handoff"))
    log.info("cycle done in %.1fs: %d inbound processed, %d handed off, "
             "%d nudged", time.time() - started, len(sent), handed,
             len(nudged))
    for s in sent:
        log.info("sent: %s action=%s guid=%s turn=%s handoff=%s note=%s",
                 s.get("sender"), s.get("action"), s.get("guid"),
                 s.get("turn_no"), s.get("handoff"), s.get("note"))
    return result


def build_gm(cfg):
    """gm per ATFL_GM (mock default; RosterGM for 'roster'). Called once
    at startup."""
    if cfg["gm"] == "roster":
        return RosterGM(node_name=cfg["a8s_node"],
                        node_root=cfg["a8s_node_root"])
    return MockGM()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Above the Fog Line poll loop")
    ap.add_argument("--once", action="store_true",
                    help="run one poll cycle, then exit")
    ap.add_argument("--fake", action="store_true",
                    help="smoke test: FakeGmail + MockGM, no `a8s tell`")
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

    relay = None
    if args.fake:
        relay = FakeGmail(murph_node=cfg["murph_node"],
                          engine_node=cfg["a8s_node"])
        gm = MockGM()
        log.info("FAKE MODE: no `a8s tell`, nothing leaves the process")
    else:
        gm = build_gm(cfg)
        log.info("polling a8s inbox of %s every %d min (turn length %d min),"
                 " handing off to murph node %s",
                 cfg["a8s_node"], cfg["poll_min"], cfg["turn_len_min"],
                 cfg["murph_node"])

    run_once(relay, gm, cfg)
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
            run_once(relay, gm, cfg)
        except Exception:
            log.error("cycle failed; loop continues\n%s",
                      traceback.format_exc())
    log.info("poll loop stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
