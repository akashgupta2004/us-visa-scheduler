n"""
=============================================================
  Orchestrator — Multi-Account Visa Bot Manager
  ─────────────────────────────────────────────────────────
  HOW TO USE:
      python main.py              — Start the orchestrator (all accounts)
      python main.py --no-monitor — Start without the slot monitor

  Reads accounts.json and for each account:
    1. Assigns a unique Chrome CDP port (9222, 9223, …)
    2. Spawns login_runner.py  — opens Chrome, logs in, stays open
    3. When login_runner.py prints [READY], spawns booking_runner.py
       — connects to that same Chrome, parks on the portal
         and watches for trigger_<customer>.json

  Also runs monitor_runner.py in the background
  to write trigger files when valid slots are found.

  Press Ctrl+C to gracefully shut down all child processes.
=============================================================
"""

import argparse
import os
import subprocess
import sys
import time
import threading
from datetime import datetime
import signal
from pathlib import Path
import queue

from dotenv import load_dotenv

# Ensure project root is on the path for top-level imports (slack.py)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from slack import send as slack_send
from src.common.utils import safe_id
from src.common.config import load_accounts as _load_accounts, ACCOUNTS_FILE
from src.common.state import update_state as _update_bot_state, get_state_file as _get_state_file

# ─────────────────────────────────────────────────────────────
BOT_SCRIPT      = Path(__file__).parent / "login_runner.py"
BOT2_SCRIPT     = Path(__file__).parent / "booking_runner.py"
MONITOR_SCRIPT  = Path(__file__).parent / "monitor_runner.py"

BASE_CDP_PORT   = 9222   # first account gets this port; each subsequent +1
PYTHON          = sys.executable

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def load_accounts() -> list[dict]:
    """Load accounts using the shared config loader."""
    return _load_accounts()


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [ORCHESTRATOR] {msg}", flush=True)


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
        stdin=subprocess.DEVNULL,
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
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
    )


def spawn_monitor() -> subprocess.Popen:
    """Launch the slot monitor in the background."""
    log("▶  Starting slot monitor …")
    cmd = [PYTHON, str(MONITOR_SCRIPT)]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
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
            if "LISTENING" not in line:
                continue
            parts = line.split()
            # parts[1] is the local address column, e.g. "127.0.0.1:9222"
            if len(parts) >= 5 and parts[1].endswith(f":{cdp_port}"):
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



def stdin_listener(q: queue.Queue):
    for line in sys.stdin:
        if line.strip():
            q.put(line.strip())

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-monitor", action="store_true", help="Disable the slot monitor")
    args = parser.parse_args()

    accounts = load_accounts()
    log(f"Loaded {len(accounts)} account(s) from accounts.json")

    all_procs: list[subprocess.Popen] = []
    procs_lock = threading.Lock()

    def shutdown(signum=None, frame=None):
        log("Shutting down all child processes …")
        for session in sessions:
            kill_chrome_by_port(session.get("cdp_port"))

        for p in all_procs:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
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

    # ── Session list ───────────────────────────────────────────
    sessions: list[dict] = []

    # ─────────────────────────────────────────────────────────────
    # Inner helpers (close over sessions / all_procs / procs_lock)
    # ─────────────────────────────────────────────────────────────

    def wait_and_spawn_booking_runner(session: dict, expected_event: threading.Event, expected_proc) -> None:
        """Background thread: waits for [READY] then spawns booking_runner."""
        customer = session["account"]["customer_name"]
        log(f"⏳  Waiting for '{customer}' login to complete …")
        if expected_event.wait(timeout=600):
            if session.get("ready_event") is not expected_event or session.get("login_proc") is not expected_proc:
                return  # Phantom thread from an older run
            log(f"✅  '{customer}' is logged in — starting bot2")
            booking_proc = spawn_booking_runner(session["account"], session["cdp_port"])
            session["booking_proc"] = booking_proc
            with procs_lock:
                all_procs.append(booking_proc)
            threading.Thread(
                target=relay_output,
                args=(booking_proc, f"booking:{customer}"),
                daemon=True,
            ).start()
        else:
            if session.get("ready_event") is not expected_event or session.get("login_proc") is not expected_proc:
                return  # Phantom thread from an older run
            log(f"⚠️  '{customer}' did not log in within 10 minutes — skipping bot2")

    def start_bot_session(sess_dict: dict) -> None:
        """Spawn login_runner and wire up relay + waiter threads."""
        uid = safe_id(sess_dict["account"]["username"])
        c_name = sess_dict["account"].get("customer_name") or uid
        p_dir = str(Path(__file__).parent.parent / f"chrome_profile_{uid}")

        new_proc = spawn_login_runner(sess_dict["account"], sess_dict["cdp_port"], p_dir)
        with procs_lock:
            all_procs.append(new_proc)
        sess_dict["login_proc"] = new_proc

        new_ready_event = threading.Event()
        sess_dict["ready_event"] = new_ready_event

        threading.Thread(
            target=relay_output,
            args=(new_proc, f"login:{c_name}", new_ready_event),
            daemon=True,
        ).start()

        threading.Thread(
            target=wait_and_spawn_booking_runner,
            args=(sess_dict, new_ready_event, new_proc),
            daemon=True,
        ).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Crash handlers run in their own background threads.
    # ─────────────────────────────────────────────────────────────────────────

    def handle_login_crash(session: dict, code: int) -> None:
        """Runs in a background thread — sleeping here does NOT block the main loop."""
        customer = session["account"]["customer_name"]

        if code == 99:
            log(f"🛑 Chrome window for '{customer}' was manually closed. Aborting auto-restart.")
            return

        log(f"🔄 Restarting login for '{customer}' after crash …")

        now = time.time()
        history = session.setdefault("login_restart_history", [])
        history.append(now)
        history[:] = [t for t in history if now - t < 300]
        if len(history) > 3:
            log(f"⚠️  Too many rapid login restarts for '{customer}'. Waiting 60s...")
            time.sleep(60)
            history.clear()
            
            if session.get("intended_state") == "STOPPED":
                log(f"⏭️  Skipping login restart for '{customer}' — was manually stopped.")
                return
            if session.get("login_proc") is not None or session.get("booking_proc") is not None:
                log(f"⏭️  Skipping login restart for '{customer}' — already running.")
                return

        kill_chrome_by_port(session["cdp_port"])
        time.sleep(4)
        
        if session.get("intended_state") == "STOPPED":
            return
            
        start_bot_session(session)

    def handle_booking_crash(session: dict, code: int) -> None:
        """Runs in a background thread — sleeping here does NOT block the main loop."""
        customer = session["account"]["customer_name"]

        if code == 42:
            log(f"⚠️  booking:{customer} encountered 429 Too Many Requests. Restarting in 15 minutes...")
            # Set an explicit flag so delayed_restart can distinguish a
            # 429 cooldown from a manual STOP.
            session["rate_limited"] = True

            def delayed_restart(sess_dict):
                time.sleep(15 * 60)
                c_name = sess_dict["account"]["customer_name"]
                # If something else already brought the bot back up, skip
                if sess_dict.get("login_proc") is not None or sess_dict.get("booking_proc") is not None:
                    log(f"⏭️  Skipping delayed restart for '{c_name}' — already running.")
                    sess_dict["rate_limited"] = False
                    return
                # STOP command sets intended_state="STOPPED" → skip the restart
                if sess_dict.get("intended_state") == "STOPPED":
                    log(f"⏭️  Skipping delayed restart for '{c_name}' — was manually stopped.")
                    sess_dict["rate_limited"] = False
                    return
                log(f"🔄 Restarting bot for '{c_name}' after 15m delay ...")
                sess_dict["rate_limited"] = False
                kill_chrome_by_port(sess_dict["cdp_port"])
                time.sleep(4)
                start_bot_session(sess_dict)

            threading.Thread(target=delayed_restart, args=(session,), daemon=True).start()
        else:
            # Session expiry or unexpected booking crash — restart immediately
            log(f"⚠️  booking:{customer} exited with code {code} — restarting bot …")

            now = time.time()
            history = session.setdefault("booking_restart_history", [])
            history.append(now)
            history[:] = [t for t in history if now - t < 300]
            if len(history) > 3:
                log(f"⚠️  Too many rapid booking restarts for '{customer}'. Waiting 60s...")
                time.sleep(60)
                history.clear()
                
                if session.get("intended_state") == "STOPPED":
                    log(f"⏭️  Skipping booking restart for '{customer}' — was manually stopped.")
                    return
                if session.get("login_proc") is not None or session.get("booking_proc") is not None:
                    log(f"⏭️  Skipping booking restart for '{customer}' — already running.")
                    return

            kill_chrome_by_port(session["cdp_port"])
            time.sleep(4)
            
            if session.get("intended_state") == "STOPPED":
                return
                
            start_bot_session(session)

    # ── Role Enforcement (Split Distributed Setup) ──────────────
    load_dotenv(Path(__file__).parent.parent / ".env")
    
    remote_trigger_url = os.getenv("REMOTE_TRIGGER_URL", "").strip()
    laptop_role = os.getenv("LAPTOP_ROLE", "").strip().upper()
    
    # Fallback for old .env files before LAPTOP_ROLE was added
    if not laptop_role:
        if remote_trigger_url:
            laptop_role = "POLLING"
        else:
            laptop_role = "ALL_IN_ONE"
            
    valid_accounts = []
    
    for account in accounts:
        role = account.get("role", "POLLING_ONLY")
        c_name = account.get("customer_name") or account.get("username", "Unknown")
        
        if laptop_role == "POLLING":
            # Polling Laptop: Only run POLLING_ONLY
            if role == "RESERVED_BOOKING":
                log(f"⏭️ Skipping VIP account '{c_name}' on Polling Laptop.")
                continue
        elif laptop_role == "BOOKING":
            # Booking Laptop: Only run RESERVED_BOOKING
            if role == "POLLING_ONLY":
                log(f"⏭️ Skipping Polling account '{c_name}' on Booking Laptop.")
                continue
        elif laptop_role == "ALL_IN_ONE":
            # Run everything
            pass
            
        valid_accounts.append(account)
        
    if not valid_accounts:
        log("❌ No valid accounts to run based on current role settings!")

    # ── Spawn one session per account ─────────────────────────
    for idx, account in enumerate(valid_accounts):
        cdp_port = BASE_CDP_PORT + idx
        sess_dict = {
            "account":               account,
            "cdp_port":              cdp_port,
            "login_proc":            None,
            "booking_proc":          None,
            "ready_event":           None,
            "login_restart_history":    [],
            "booking_restart_history":  [],
            "rate_limited":             False,   # Explicit 429 flag
            "intended_state":           "RUNNING",
        }
        sessions.append(sess_dict)
        start_bot_session(sess_dict)

    # ── Start slot monitor ────────────────────────────────────
    # Track monitor_proc in a mutable dict so the main loop can detect crashes.
    monitor_state = {"proc": None}
    
    # Do not start monitor on BOOKING laptop (Polling laptop handles it)
    should_run_monitor = not args.no_monitor
    if laptop_role == "BOOKING":
        log("⏭️ Skipping Slot Monitor on Booking Laptop (Polling Laptop will handle CVS).")
        should_run_monitor = False
        
        # Start the webhook receiver to listen for remote triggers from the Polling Laptop
        def spawn_webhook():
            env = os.environ.copy()
            return subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).parent / "webhook_receiver.py")],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace"
            )
        wp = spawn_webhook()
        with procs_lock:
            all_procs.append(wp)
        threading.Thread(target=relay_output, args=(wp, "webhook"), daemon=True).start()

    if should_run_monitor:
        mp = spawn_monitor()
        monitor_state["proc"] = mp
        with procs_lock:
            all_procs.append(mp)
        threading.Thread(target=relay_output, args=(mp, "monitor"), daemon=True).start()

    log("="*60)
    log("All processes launched. Press Ctrl+C to stop everything.")
    log("="*60)

    # ── Keep the main thread alive ────────────────────────────
    cmd_queue = queue.Queue()
    threading.Thread(target=stdin_listener, args=(cmd_queue,), daemon=True).start()

    try:
        while True:
            # ── Process GUI/CLI commands from stdin ───────────────────
            try:
                cmd_str = cmd_queue.get(timeout=5)
                if cmd_str.startswith("STOP:"):
                    uid = cmd_str.split(":")[1]
                    for session in sessions:
                        if safe_id(session["account"]["username"]) == uid:
                            cname = session["account"].get("customer_name") or uid
                            log(f"🛑 UI requested shutdown for '{cname}'")
                            proc  = session.get("login_proc")
                            bproc = session.get("booking_proc")
                            if bproc and bproc.poll() is None:
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(bproc.pid)], capture_output=True)
                            if proc and proc.poll() is None:
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
                            kill_chrome_by_port(session["cdp_port"])
                            session["login_proc"]  = None
                            session["booking_proc"] = None
                            session["ready_event"]  = None
                            session["intended_state"] = "STOPPED"
                            # Clear flag so any in-flight delayed_restart thread aborts
                            session["rate_limited"] = False
                            try:
                                _update_bot_state(_get_state_file(session["account"]["username"]), {"extension_running": False, "pending": False})
                            except Exception:
                                pass
                            break
                elif cmd_str.startswith("START:"):
                    uid = cmd_str.split(":")[1]
                    for session in sessions:
                        if safe_id(session["account"]["username"]) == uid:
                            if session.get("login_proc") is not None or session.get("booking_proc") is not None:
                                break  # already running
                            cname = session["account"].get("customer_name") or uid
                            log(f"▶️ UI requested start for '{cname}'")
                            session["intended_state"] = "RUNNING"
                            start_bot_session(session)
                            break
            except queue.Empty:
                pass

            # ── Clean up dead processes from all_procs list ───────────
            with procs_lock:
                all_procs[:] = [p for p in all_procs if p.poll() is None]

            # ── Check if monitor crashed ──────────────────────────
            if not args.no_monitor and monitor_state["proc"] is not None:
                ret = monitor_state["proc"].poll()
                if ret is not None:
                    log(f"⚠️  Slot monitor crashed (code {ret}). Restarting in 5s …")
                    time.sleep(5)
                    mp = spawn_monitor()
                    monitor_state["proc"] = mp
                    with procs_lock:
                        all_procs.append(mp)
                    threading.Thread(target=relay_output, args=(mp, "monitor"), daemon=True).start()

            # ── Health-check each bot session ─────────────────────────
            for session in sessions:
                proc         = session.get("login_proc")
                booking_proc = session.get("booking_proc")
                customer     = session["account"]["customer_name"]

                # --- Login runner exited ---
                if proc and proc.poll() is not None:
                    code = proc.returncode
                    log(f"⚠️  login:{customer} exited with code {code}")
                    session["login_proc"] = None
                    # Always kill booking_proc when login dies
                    if booking_proc and booking_proc.poll() is None:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(booking_proc.pid)], capture_output=True)
                    session["booking_proc"] = None
                    try:
                        _update_bot_state(_get_state_file(session["account"]["username"]), {"extension_running": False})
                    except Exception:
                        pass
                    # Hand off to background thread — main loop is never blocked
                    threading.Thread(target=handle_login_crash, args=(session, code), daemon=True).start()

                # --- Booking runner exited ---
                # Re-read from session dict since the login handler above may have already cleared it.
                current_booking_proc = session.get("booking_proc")
                if current_booking_proc and current_booking_proc.poll() is not None:
                    code = current_booking_proc.returncode
                    session["booking_proc"] = None
                    try:
                        _update_bot_state(_get_state_file(session["account"]["username"]), {"extension_running": False})
                    except Exception:
                        pass
                    # Re-fetch login_proc from session instead of using the stale
                    # local `proc` captured at the top of this loop iteration.
                    current_login_proc = session.get("login_proc")
                    if current_login_proc and current_login_proc.poll() is None:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(current_login_proc.pid)], capture_output=True)
                    session["login_proc"] = None
                    # Hand off to background thread — main loop is never blocked
                    threading.Thread(target=handle_booking_crash, args=(session, code), daemon=True).start()

    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
