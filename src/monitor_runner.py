"""
=============================================================
  Monitor Runner — Polling & Analytics Trigger
  ─────────────────────────────────────────────────────────
  Entrypoint runner that polls the visa slot API, records slots history
  for analysis, and writes a trigger file to activate the booking runner.
=============================================================
"""

import time
import random
import json
import hashlib
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Force UTF-8 output so emojis don't crash on Windows when piped
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Ensure project root is on the path for top-level imports (slack.py) and src.* packages
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.monitor.api import fetch_rows
from src.monitor.matcher import build_buckets, find_valid_ofc_slot, find_valid_consular_slot
from src.monitor.notifier import log_slots_for_analysis
from src.common.utils import safe_id
from src.common.config import ACCOUNTS_FILE, SLOT_ALERT_STATE_FILE, normalize_city, parse_date
from src.common.state import read_state as _read_bot_state, update_state as _update_bot_state
from slack import format_slack_message, send_slack, send_slack_error

ALERT_COOLDOWN_SECONDS = 15 * 60
ERROR_BACKOFF_SECONDS = 40

POLL_MIN_SECONDS = 5
POLL_MAX_SECONDS = 15

def load_state():
    if not SLOT_ALERT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(SLOT_ALERT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state):
    SLOT_ALERT_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

def make_alert_key(uid, ofc_city, consular_city, ofc_date, consular_date):
    raw = f"{uid}|{ofc_city}|{consular_city}|{ofc_date}|{consular_date}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

def should_alert(alert_key, state):
    last_sent = state.get(alert_key, 0)
    return (time.time() - last_sent) > ALERT_COOLDOWN_SECONDS

def mark_alert(alert_key, state):
    state[alert_key] = time.time()


def load_customers():
    if not ACCOUNTS_FILE.exists():
        raise FileNotFoundError(f"accounts.json not found at {ACCOUNTS_FILE}.")
    with ACCOUNTS_FILE.open(encoding="utf-8") as f:
        raw = json.load(f)

    required_fields_sniper = [
        "customer_name",
        "ofcCities",
        "consularCities",
        "ofcStartDate",
        "ofcEndDate",
        "consularStartDate",
        "consularEndDate",
    ]
    required_fields_reschedule = [
        "customer_name",
        "consularCities",
        "consularStartDate",
        "consularEndDate",
    ]
    customers = []
    for entry in raw:
        username = str(entry.get("username", "")).strip()
        if not username:
            print("⚠️ Skipping account without username.")
            continue
            
        customer_name = str(entry.get("customer_name", "")).strip() or username

        action_mode = entry.get("action_mode", "SNIPER")
        required_fields = required_fields_reschedule if action_mode == "RESCHEDULE_CONSULAR" else required_fields_sniper

        missing = [k for k in required_fields if k not in entry or entry[k] == ""]
        if missing:
            raise ValueError(f"accounts.json entry for '{customer_name}' is missing: {missing}")

        ofc_cities = entry.get("ofcCities", [])
        consular_cities = entry["consularCities"]
        ofc_start = entry.get("ofcStartDate", None)
        ofc_end = entry.get("ofcEndDate", None)
        consular_start = entry["consularStartDate"]
        consular_end = entry["consularEndDate"]

        customers.append({
            "username":      username,
            "uid":           safe_id(username),
            "customer_name": customer_name,
            "action_mode":   action_mode,
            "ofc_cities":      [normalize_city(c) for c in ofc_cities],
            "consular_cities": [normalize_city(c) for c in consular_cities],
            "ofc_start":       parse_date(ofc_start),
            "ofc_end":         parse_date(ofc_end),
            "consular_start":  parse_date(consular_start),
            "consular_end":    parse_date(consular_end),
            "prevent_immediate": entry.get("prevent_immediate", False)
        })

    return customers


def _get_effective_dates(customer):
    """Compute effective start dates for a customer, respecting prevent_immediate.
    
    Returns (effective_ofc_start, effective_consular_start) without mutating
    the customer dict so the original dates are preserved across poll cycles.
    """
    ofc_start = customer["ofc_start"]
    consular_start = customer["consular_start"]

    if customer.get("prevent_immediate"):
        dynamic_start = datetime.today() + timedelta(days=3)
        dynamic_start = dynamic_start.replace(hour=0, minute=0, second=0, microsecond=0)
        
        if not ofc_start or ofc_start < dynamic_start:
            ofc_start = dynamic_start
        if not consular_start or consular_start < dynamic_start:
            consular_start = dynamic_start

    return ofc_start, consular_start


def main():
    print(f"Running qualified slot monitor (interval ~{POLL_MIN_SECONDS}s, unlimited fetches)...")
    state = load_state()

    while True:
        # Bug 7 fix: reload customers on every iteration to pick up GUI changes
        customers = load_customers()

        try:
            rows = fetch_rows()
            if not rows:
                print("⚠️ API returned empty slot data (no rows). Monitor is still running, waiting for next poll...")
                time.sleep(random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS))
                continue

            logged = log_slots_for_analysis(rows)

            ofc_buckets, consular_buckets = build_buckets(rows)

            for customer in customers:
                customer_name = customer["customer_name"]
                action_mode = customer["action_mode"]
                uid = customer["uid"]
                state_file = Path(__file__).parent / f"state_{uid}.json"
                bot_state = _read_bot_state(state_file)

                # Compute effective dates without mutating the customer dict
                # so original dates are preserved across poll cycles
                effective_ofc_start, effective_consular_start = _get_effective_dates(customer)
                
                if bot_state.get("waitingForConsular"):
                    # ── Fallback Consular-Only path (Post-OFC) ────────────────
                    booked_ofc_date_str = bot_state.get("bookedOfcDate")
                    if booked_ofc_date_str:
                        booked_date_obj = parse_date(booked_ofc_date_str)
                        if booked_date_obj:
                            # Consular date must be strictly after the OFC date
                            effective_consular_start = max(effective_consular_start, booked_date_obj + timedelta(days=1))
                            
                    consular_slot, matched_consular_city = find_valid_consular_slot(
                        consular_buckets,
                        customer["consular_cities"],
                        effective_consular_start,
                        customer["consular_end"],
                    )

                    if not consular_slot:
                        continue

                    alert_key = make_alert_key(
                        uid, "", matched_consular_city,
                        "", consular_slot["display_date"],
                    )

                    if not should_alert(alert_key, state):
                        continue

                    send_slack(
                        format_slack_message(
                            customer,
                            None,
                            consular_slot,
                            None,
                            matched_consular_city,
                        )
                    )
                    mark_alert(alert_key, state)

                    print(
                        f"✅ [FALLBACK] Alert sent for {customer_name} | "
                        f"Consular {matched_consular_city} {consular_slot['display_date']} ({consular_slot['count']} slots)"
                    )

                    if bot_state.get("extension_running"):
                        print(f"⏭️  Extension already running for '{customer_name}' — skipping.")
                        continue

                    if bot_state.get("pending"):
                        print(f"⚠️ Overwriting unhandled pending trigger for '{customer_name}' with newer fallback slot.")

                    _update_bot_state(state_file, {
                        "extension_running": False,
                        "pending": True,
                        "trigger_timestamp": time.time(),
                        "action_type": "SNIPER_CONSULAR_ONLY",
                        "consularCities": customer["consular_cities"],
                        "consularPriorityCity": matched_consular_city,
                        "consularStartDate": effective_consular_start.strftime("%Y-%m-%d"),
                        "consularEndDate": customer["consular_end"].strftime("%Y-%m-%d"),
                        "customer_name": customer_name,
                        "prevent_immediate": customer.get("prevent_immediate", False),
                    })
                    print(f"✅ Consular-Only trigger queued for '{customer_name}'.")
                    continue

                if action_mode == "RESCHEDULE_CONSULAR":
                    # ── Consular Reschedule Only path ────────────────────────────
                    consular_slot, matched_consular_city = find_valid_consular_slot(
                        consular_buckets,
                        customer["consular_cities"],
                        effective_consular_start,
                        customer["consular_end"],
                    )

                    if not consular_slot:
                        continue

                    alert_key = make_alert_key(
                        uid, "", matched_consular_city,
                        "", consular_slot["display_date"],
                    )

                    if not should_alert(alert_key, state):
                        continue

                    send_slack(
                        format_slack_message(
                            customer,
                            None,
                            consular_slot,
                            None,
                            matched_consular_city,
                        )
                    )
                    mark_alert(alert_key, state)

                    print(
                        f"✅ [RESCHEDULE] Alert sent for {customer_name} | "
                        f"Consular {matched_consular_city} {consular_slot['display_date']} ({consular_slot['count']} slots)"
                    )

                    state_file = Path(__file__).parent / f"state_{uid}.json"
                    # Re-read bot state just in case it changed during Slack formatting
                    bot_state = _read_bot_state(state_file)

                    if bot_state.get("extension_running"):
                        print(f"⏭️  Extension already running for '{customer_name}' — skipping.")
                        continue

                    if bot_state.get("pending"):
                        print(f"⚠️ Overwriting unhandled pending trigger for '{customer_name}' with newer reschedule slot.")

                    _update_bot_state(state_file, {
                        "extension_running": False,
                        "pending": True,
                        "trigger_timestamp": time.time(),
                        "action_type": "RESCHEDULE_CONSULAR",
                        "consularCities": customer["consular_cities"],
                        "consularPriorityCity": matched_consular_city,
                        "consularStartDate": effective_consular_start.strftime("%Y-%m-%d"),
                        "consularEndDate": customer["consular_end"].strftime("%Y-%m-%d"),
                        "customer_name": customer_name,
                        "prevent_immediate": customer.get("prevent_immediate", False),
                    })
                    print(f"✅ Reschedule trigger queued for '{customer_name}'.")

                else:
                    # ── Full Booking (SNIPER) path ─────────────────────────────────
                    ofc, matched_ofc_city = find_valid_ofc_slot(
                        ofc_buckets,
                        customer["ofc_cities"],
                        effective_ofc_start,
                        customer["ofc_end"],
                    )

                    if not ofc:
                        continue

                    consular_min_date = max(effective_consular_start, ofc["date"] + timedelta(days=1))
                    consular, matched_consular_city = find_valid_consular_slot(
                        consular_buckets,
                        customer["consular_cities"],
                        consular_min_date,
                        customer["consular_end"],
                    )
                    
                    if consular:
                        action_type = "SNIPER"
                        consular_desc = f"{matched_consular_city} {consular['display_date']} ({consular['count']} slots)"
                    else:
                        action_type = "SNIPER"
                        consular_desc = "pending (wait mode)"
                        matched_consular_city = customer["consular_cities"][0] if customer["consular_cities"] else ""

                    alert_key = make_alert_key(
                        uid,
                        matched_ofc_city,
                        "",
                        ofc["display_date"],
                        "",
                    )

                    if not should_alert(alert_key, state):
                        continue

                    send_slack(
                        format_slack_message(
                            customer,
                            ofc,
                            consular,
                            matched_ofc_city,
                            matched_consular_city if consular else None,
                        )
                    )
                    mark_alert(alert_key, state)

                    print(
                        f"✅ Alert sent for {customer_name} | "
                        f"OFC {matched_ofc_city} {ofc['display_date']} ({ofc['count']} slots) | "
                        f"Consular {consular_desc}"
                    )

                    # ── Signal bot2 to book immediately ────────────────────────
                    state_file = Path(__file__).parent / f"state_{uid}.json"

                    bot_state = _read_bot_state(state_file)

                    if bot_state.get("extension_running"):
                        print(f"⏭️  Extension already running for '{customer_name}' — skipping.")
                        continue

                    if bot_state.get("pending"):
                        print(f"⚠️ Overwriting unhandled pending trigger for '{customer_name}' with newer sniper slot.")

                    _update_bot_state(state_file, {
                        "extension_running": False,
                        "pending": True,
                        "trigger_timestamp": time.time(),
                        "action_type": action_type,
                        "ofcCities": customer["ofc_cities"],
                        "ofcPriorityCity": matched_ofc_city,
                        "ofcStartDate": effective_ofc_start.strftime("%Y-%m-%d"),
                        "ofcEndDate": customer["ofc_end"].strftime("%Y-%m-%d"),
                        "consularCities": customer["consular_cities"],
                        "consularPriorityCity": matched_consular_city,
                        "consularStartDate": effective_consular_start.strftime("%Y-%m-%d"),
                        "consularEndDate": customer["consular_end"].strftime("%Y-%m-%d"),
                        "customer_name": customer_name,
                        "prevent_immediate": customer.get("prevent_immediate", False),
                    })
                    print(f"✅ Trigger queued for '{customer_name}' — booking runner will pick it up.")


            save_state(state)
        except Exception as e:
            print(f"❌ Error: {e}")
            last_err_time = state.get("last_slack_error_time", 0)
            if time.time() - last_err_time > ALERT_COOLDOWN_SECONDS:
                try:
                    send_slack_error(f"Error in slot monitor: {e}")
                    state["last_slack_error_time"] = time.time()
                    save_state(state)
                except Exception as slack_e:
                    print(f"❌ Failed to send Slack error notification: {slack_e}")
            else:
                print("⏳ Slack error skipped (cooldown active).")
            time.sleep(ERROR_BACKOFF_SECONDS)
            continue

        time.sleep(random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS))


if __name__ == "__main__":
    main()
