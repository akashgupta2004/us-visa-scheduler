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
from datetime import datetime
import signal
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from slack import send as slack_send

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


def safe_id(username: str) -> str:
    """Generate a filesystem-safe unique identifier from a username/email."""
    return re.sub(r'[^a-zA-Z0-9]', '_', str(username))


# ─────────────────────────────────────────────────────────────
# Process launchers
# ─────────────────────────────────────────────────────────────

def spawn_login_runner(account: dict, cdp_port: int, profile_dir: str) -> subprocess.Popen:
    """Launch login_runner.py for a single account."""
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
        encoding="utf-8",
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
    )


def spawn_booking_runner(account: dict, cdp_port: int) -> subprocess.Popen:
    """Launch booking_runner.py for a single account once login is done."""
    customer = account.get("customer_name") or account["username"]
    cmd = [
        PYTHON, str(BOT2_SCRIPT),
        "--cdp-port", str(cdp_port),
        "--customer",  customer,
        "--username",  account["username"]
    ]
    log(f"▶  Starting bot2 for '{customer}' on port {cdp_port}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
    )


def spawn_monitor(max_fetches: int | None = None) -> subprocess.Popen:
    """Launch the slot monitor in the background."""
    log("▶  Starting slot monitor …")
    cmd = [PYTHON, str(MONITOR_SCRIPT)]
    if max_fetches:
        cmd.extend(["--max-fetches", str(max_fetches)])
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
    )


def kill_chrome_by_port(cdp_port: int) -> None:
    """Kill the Chrome process listening on the given CDP port."""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True
        )
        pid = None
        for line in result.stdout.splitlines():
            if f":{cdp_port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1]
                break
        if pid and pid.isdigit():
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
            log(f"🖥️  Killed Chrome PID {pid} on port {cdp_port}")
        else:
            log(f"⚠️  No Chrome process found on port {cdp_port} to kill")
    except Exception as e:
        log(f"⚠️  Failed to kill Chrome on port {cdp_port}: {e}")


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
            if label == "monitor":
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
                print(f"[monitor] {ts} {line}", flush=True)
            else:
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
    parser.add_argument("--max-fetches", type=int, default=None, help="Max number of times the monitor will fetch from the API")
    args = parser.parse_args()

    accounts = load_accounts()
    log(f"Loaded {len(accounts)} account(s) from accounts.json")
    if args.max_fetches is not None:
        log(f"API fetch limit set to: {args.max_fetches}")

    all_procs: list[subprocess.Popen] = []

    def shutdown(signum=None, frame=None):
        log("Shutting down all child processes …")
        for session in sessions:
            kill_chrome_by_port(session.get("cdp_port"))

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
        customer    = account.get("customer_name") or account["username"]
        uid         = safe_id(account["username"])
        profile_dir = str(Path(__file__).parent.parent / f"chrome_profile_{uid}")

        login_proc = spawn_login_runner(account, cdp_port, profile_dir)
        all_procs.append(login_proc)

        # Event that fires when bot.py prints [READY]
        ready_event = threading.Event()

        # Relay bot output; watch for [READY]
        t = threading.Thread(
            target=relay_output,
            args=(login_proc, f"login:{customer}", ready_event),
            daemon=True,
        )
        t.start()

        sessions.append({
            "account":     account,
            "cdp_port":    cdp_port,
            "login_proc":    login_proc,
            "ready_event": ready_event,
            "restart_history": [],
        })

    # ── Wait for each bot to log in, then start bot2 ──────────
    def wait_and_spawn_booking_runner(session: dict) -> None:
        customer = session["account"]["customer_name"]
        log(f"⏳  Waiting for '{customer}' login to complete …")
        # Wait up to 10 minutes for login
        if session["ready_event"].wait(timeout=600):
            log(f"✅  '{customer}' is logged in — starting bot2")
            booking_proc = spawn_booking_runner(session["account"], session["cdp_port"])
            session["booking_proc"] = booking_proc
            all_procs.append(booking_proc)
            relay_thread = threading.Thread(
                target=relay_output,
                args=(booking_proc, f"booking:{customer}"),
                daemon=True,
            )
            relay_thread.start()
        else:
            log(f"⚠️  '{customer}' did not log in within 10 minutes — skipping bot2")

    watcher_threads = []
    for session in sessions:
        wt = threading.Thread(target=wait_and_spawn_booking_runner, args=(session,), daemon=True)
        wt.start()
        watcher_threads.append(wt)

    # ── Start slot monitor ────────────────────────────────────
    if not args.no_monitor:
        monitor_proc = spawn_monitor(args.max_fetches)
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
            
            # Clean up dead processes from all_procs
            all_procs[:] = [p for p in all_procs if p.poll() is None]

            # Check for individual bot stop requests or unexpected exits
            for session in sessions:
                proc = session.get("login_proc")
                booking_proc = session.get("booking_proc")
                customer = session["account"]["customer_name"]
                safe_name = customer.replace(' ', '_')
                
                # If UI requested to stop this bot
                stop_file = Path(__file__).parent / f".stop_{safe_name}"
                if stop_file.exists():
                    log(f"🛑 UI requested shutdown for '{customer}'")
                    kill_chrome_by_port(session["cdp_port"])
                    if booking_proc and booking_proc.poll() is None:
                        booking_proc.terminate()
                    if proc and proc.poll() is None:
                        proc.terminate()
                    stop_file.unlink(missing_ok=True)
                    session["login_proc"] = None
                    session["booking_proc"] = None
                    continue

                if proc and proc.poll() is not None:
                    code = proc.returncode
                    log(f"⚠️  login:{customer} exited with code {code}")
                    session["login_proc"] = None
                    if booking_proc and booking_proc.poll() is None:
                        booking_proc.terminate()
                        session["booking_proc"] = None
                        
                    if code == 99:
                        log(f"🛑 Chrome window for '{customer}' was manually closed. Aborting auto-restart.")
                        continue
                        
                    # ── Auto-restart on login crash ───────────────────────
                    log(f"🔄 Restarting login for '{customer}' after crash …")
                    
                    now = time.time()
                    history = session.setdefault("restart_history", [])
                    history.append(now)
                    history[:] = [t for t in history if now - t < 300]
                    if len(history) > 3:
                        log(f"⚠️  Too many rapid restarts for '{customer}'. Waiting 60s...")
                        time.sleep(60)
                        history.clear()
                        
                    kill_chrome_by_port(session["cdp_port"])
                    time.sleep(4)
                    p_dir = str(Path(__file__).parent.parent / f"chrome_profile_{customer}")
                    new_proc = spawn_login_runner(session["account"], session["cdp_port"], p_dir)
                    all_procs.append(new_proc)
                    session["login_proc"] = new_proc
                    new_ready_event = threading.Event()
                    session["ready_event"] = new_ready_event
                    threading.Thread(
                        target=relay_output,
                        args=(new_proc, f"login:{customer}", new_ready_event),
                        daemon=True,
                    ).start()
                    threading.Thread(
                        target=wait_and_spawn_booking_runner,
                        args=(session,),
                        daemon=True,
                    ).start()

                if booking_proc and booking_proc.poll() is not None:
                    code = booking_proc.returncode
                    session["booking_proc"] = None
                    if code == 42:
                        log(f"⚠️  booking:{customer} encountered 429 Too Many Requests. Restarting in 25 minutes...")
                        if proc and proc.poll() is None:
                            proc.terminate()
                        session["login_proc"] = None

                        def delayed_restart(sess_dict):
                            time.sleep(25 * 60)
                            c_name = sess_dict["account"]["customer_name"]
                            log(f"🔄 Restarting bot for '{c_name}' after 25m delay ...")
                            p_dir = str(Path(__file__).parent.parent / f"chrome_profile_{c_name}")
                            kill_chrome_by_port(sess_dict["cdp_port"])
                            time.sleep(4)
                            new_proc = spawn_login_runner(sess_dict["account"], sess_dict["cdp_port"], p_dir)
                            all_procs.append(new_proc)
                            sess_dict["login_proc"] = new_proc
                            new_ready_event = threading.Event()
                            sess_dict["ready_event"] = new_ready_event
                            threading.Thread(
                                target=relay_output,
                                args=(new_proc, f"login:{c_name}", new_ready_event),
                                daemon=True,
                            ).start()
                            wait_and_spawn_booking_runner(sess_dict)

                        threading.Thread(target=delayed_restart, args=(session,), daemon=True).start()
                    else:
                        # Session expiry or unexpected booking crash — restart immediately
                        log(f"⚠️  booking:{customer} exited with code {code} — restarting bot …")
                        
                        # Don't spam Slack for routine 10-minute inactivity restarts
                        if proc and proc.poll() is None:
                            proc.terminate()
                        session["login_proc"] = None
                        
                        now = time.time()
                        history = session.setdefault("restart_history", [])
                        history.append(now)
                        history[:] = [t for t in history if now - t < 300]
                        if len(history) > 3:
                            log(f"⚠️  Too many rapid restarts for '{customer}'. Waiting 60s...")
                            time.sleep(60)
                            history.clear()
                            
                        kill_chrome_by_port(session["cdp_port"])
                        time.sleep(4)
                        p_dir = str(Path(__file__).parent.parent / f"chrome_profile_{customer}")
                        new_proc = spawn_login_runner(session["account"], session["cdp_port"], p_dir)
                        all_procs.append(new_proc)
                        session["login_proc"] = new_proc
                        new_ready_event = threading.Event()
                        session["ready_event"] = new_ready_event
                        threading.Thread(
                            target=relay_output,
                            args=(new_proc, f"login:{customer}", new_ready_event),
                            daemon=True,
                        ).start()
                        threading.Thread(
                            target=wait_and_spawn_booking_runner,
                            args=(session,),
                            daemon=True,
                        ).start()
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
