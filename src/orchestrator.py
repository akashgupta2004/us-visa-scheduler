"""
=============================================================
  Orchestrator — Multi-Account Visa Bot Manager
  ─────────────────────────────────────────────────────────
  HOW TO USE:
      python orchestrator.py

  Reads accounts.json and for each account:
    1. Assigns a unique Chrome CDP port (9222, 9223, …)
    2. Spawns login_runner.py  — opens Chrome, logs in, stays open
    3. When login_runner.py prints [READY], spawns booking_runner.py
       — connects to that same Chrome, parks on the portal
         and watches for trigger_<customer>.json

# Also runs monitor_runner.py in the background
# to write trigger files when valid slots are found.
#
# Press Ctrl+C to gracefully shut down all child processes.
# =============================================================
# """

import json
import os
import subprocess
import sys
import time
import threading
import signal
from pathlib import Path

# ─────────────────────────────────────────────────────────────
ACCOUNTS_FILE   = Path(__file__).parent.parent / "accounts.json"
BOT_SCRIPT      = Path(__file__).parent / "login_runner.py"
BOT2_SCRIPT     = Path(__file__).parent / "booking_runner.py"
MONITOR_SCRIPT  = Path(__file__).parent / "monitor_runner.py"

BASE_CDP_PORT   = 9222   # first account gets this port; each subsequent +1
PYTHON          = sys.executable

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        print(f"[ORCHESTRATOR] ❌  accounts.json not found at {ACCOUNTS_FILE}")
        sys.exit(1)
    with ACCOUNTS_FILE.open(encoding="utf-8") as f:
        accounts = json.load(f)
    if not isinstance(accounts, list) or not accounts:
        print("[ORCHESTRATOR] ❌  accounts.json must be a non-empty JSON array.")
        sys.exit(1)
    return accounts


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [ORCHESTRATOR] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────
# Process launchers
# ─────────────────────────────────────────────────────────────

def spawn_bot(account: dict, cdp_port: int, profile_dir: str) -> subprocess.Popen:
    """Launch bot.py for a single account."""
    customer = account["customer_name"]
    cmd = [
        PYTHON, str(BOT_SCRIPT),
        "--username",    account["username"],
        "--password",    account["password"],
        "--cdp-port",    str(cdp_port),
        "--customer",    customer,
        "--profile-dir", profile_dir,
    ]
    log(f"▶  Starting bot for '{customer}' on port {cdp_port}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
    )


def spawn_bot2(account: dict, cdp_port: int) -> subprocess.Popen:
    """Launch bot2_ofc_booking.py for a single account once login is done."""
    customer = account["customer_name"]
    cmd = [
        PYTHON, str(BOT2_SCRIPT),
        "--cdp-port", str(cdp_port),
        "--customer",  customer,
    ]
    log(f"▶  Starting bot2 for '{customer}' on port {cdp_port}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
    )


def spawn_monitor(interval: int | None = None) -> subprocess.Popen:
    """Launch the slot monitor in the background."""
    log("▶  Starting slot monitor …")
    cmd = [PYTHON, str(MONITOR_SCRIPT)]
    if interval:
        cmd.extend(["--interval", str(interval)])
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
    )


# ─────────────────────────────────────────────────────────────
# Log-relay thread (pipes subprocess stdout → our stdout)
# ─────────────────────────────────────────────────────────────

def relay_output(proc: subprocess.Popen, label: str, ready_event: threading.Event | None = None) -> None:
    """
    Read subprocess stdout line-by-line and re-print with a label prefix.
    If ready_event is provided, set it when '[READY]' is detected in a line.
    """
    try:
        for line in proc.stdout:
            line = line.rstrip()
            print(f"[{label}] {line}", flush=True)
            if ready_event and "[READY]" in line:
                ready_event.set()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

import argparse

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-monitor", action="store_true", help="Disable the slot monitor")
    parser.add_argument("--monitor-interval", type=int, default=None, help="Slot monitor polling interval in seconds")
    args = parser.parse_args()

    accounts = load_accounts()
    log(f"Loaded {len(accounts)} account(s) from accounts.json")

    all_procs: list[subprocess.Popen] = []

    def shutdown(signum=None, frame=None):
        log("Shutting down all child processes …")
        for p in all_procs:
            try:
                p.terminate()
            except Exception:
                pass
        # Give them a moment to die
        time.sleep(2)
        for p in all_procs:
            try:
                p.kill()
            except Exception:
                pass
        log("All done. Bye!")
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Start one bot.py per account in parallel ──────────────
    sessions: list[dict] = []

    for idx, account in enumerate(accounts):
        cdp_port    = BASE_CDP_PORT + idx
        customer    = account["customer_name"]
        profile_dir = str(Path(__file__).parent.parent / f"chrome_profile_{customer}")

        bot_proc = spawn_bot(account, cdp_port, profile_dir)
        all_procs.append(bot_proc)

        # Event that fires when bot.py prints [READY]
        ready_event = threading.Event()

        # Relay bot output; watch for [READY]
        t = threading.Thread(
            target=relay_output,
            args=(bot_proc, f"bot:{customer}", ready_event),
            daemon=True,
        )
        t.start()

        sessions.append({
            "account":     account,
            "cdp_port":    cdp_port,
            "bot_proc":    bot_proc,
            "ready_event": ready_event,
        })

    # ── Wait for each bot to log in, then start bot2 ──────────
    def wait_and_spawn_bot2(session: dict) -> None:
        customer = session["account"]["customer_name"]
        log(f"⏳  Waiting for '{customer}' login to complete …")
        # Wait up to 10 minutes for login
        if session["ready_event"].wait(timeout=600):
            log(f"✅  '{customer}' is logged in — starting bot2")
            bot2_proc = spawn_bot2(session["account"], session["cdp_port"])
            session["bot2_proc"] = bot2_proc
            all_procs.append(bot2_proc)
            relay_thread = threading.Thread(
                target=relay_output,
                args=(bot2_proc, f"bot2:{customer}"),
                daemon=True,
            )
            relay_thread.start()
        else:
            log(f"⚠️  '{customer}' did not log in within 10 minutes — skipping bot2")

    watcher_threads = []
    for session in sessions:
        wt = threading.Thread(target=wait_and_spawn_bot2, args=(session,), daemon=True)
        wt.start()
        watcher_threads.append(wt)

    # ── Start slot monitor ────────────────────────────────────
    if not args.no_monitor:
        monitor_proc = spawn_monitor(args.monitor_interval)
        all_procs.append(monitor_proc)
        threading.Thread(
            target=relay_output,
            args=(monitor_proc, "monitor"),
            daemon=True,
        ).start()

    log("="*60)
    log("All processes launched. Press Ctrl+C to stop everything.")
    log("="*60)

    # ── Keep the main thread alive ────────────────────────────
    try:
        while True:
            time.sleep(5)
            # Check for individual bot stop requests or unexpected exits
            for session in sessions:
                proc = session.get("bot_proc")
                bot2_proc = session.get("bot2_proc")
                customer = session["account"]["customer_name"]
                safe_name = customer.replace(' ', '_')
                
                # If UI requested to stop this bot
                stop_file = Path(f".stop_{safe_name}")
                if stop_file.exists():
                    log(f"🛑 UI requested shutdown for '{customer}'")
                    if bot2_proc and bot2_proc.poll() is None:
                        bot2_proc.terminate()
                    if proc and proc.poll() is None:
                        proc.terminate()
                    stop_file.unlink(missing_ok=True)
                    session["bot_proc"] = None
                    session["bot2_proc"] = None
                    continue

                if proc and proc.poll() is not None:
                    log(f"⚠️  bot:{customer} exited with code {proc.returncode}")
                    session["bot_proc"] = None
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
