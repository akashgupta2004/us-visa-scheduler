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
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Force UTF-8 output so emojis don't crash on Windows when piped
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import argparse

from src.monitor.api import fetch_rows
from src.monitor.matcher import build_buckets, find_valid_pair, find_valid_consular_slot, parse_date, normalize_city
from src.monitor.notifier import log_slots_for_analysis
from slack import format_slack_message, send_slack, send_slack_error

ACCOUNTS_FILE = Path(__file__).parent.parent / "accounts.json"
STATE_FILE = Path(__file__).parent.parent / "slot_alert_state.json"

ALERT_COOLDOWN_SECONDS = 15 * 60
ERROR_BACKOFF_SECONDS = 25

POLL_MIN_SECONDS = 15
POLL_MAX_SECONDS = 20

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

def make_alert_key(customer_name, ofc_city, consular_city, ofc_date, consular_date):
    raw = f"{customer_name}|{ofc_city}|{consular_city}|{ofc_date}|{consular_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def should_alert(alert_key, state):
    last_sent = state.get(alert_key, 0)
    return (time.time() - last_sent) > ALERT_COOLDOWN_SECONDS

def mark_alert(alert_key, state):
    state[alert_key] = time.time()

def _read_bot_state(state_file: Path) -> dict:
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_bot_state(state_file: Path, state: dict):
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

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
        customer_name = str(entry.get("customer_name", "")).strip()
        if not customer_name:
            continue

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

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-fetches", type=int, default=None, help="Max number of times to fetch from API")
    args = parser.parse_args()

    max_fetches = args.max_fetches
    fetches_count = 0

    print(f"Running qualified slot monitor (interval ~{POLL_MIN_SECONDS}s, max_fetches={max_fetches if max_fetches else 'unlimited'})...")
    state = load_state()
    customers = load_customers()

    while True:
        if max_fetches is not None and fetches_count >= max_fetches:
            print(f"🛑 Reached maximum limit of {max_fetches} API fetches. Monitor stopping.")
            break

        try:
            rows = fetch_rows()
            fetches_count += 1
            if not rows:
                print("⚠️ API returned empty slot data (no rows). Monitor is still running, waiting for next poll...")
                time.sleep(random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS))
                continue

            logged = log_slots_for_analysis(rows)

            ofc_buckets, consular_buckets = build_buckets(rows)

            for customer in customers:
                customer_name = customer["customer_name"]
                action_mode = customer["action_mode"]

                # Apply Prevent Immediate Booking logic dynamically
                if customer.get("prevent_immediate"):
                    dynamic_start = datetime.today() + timedelta(days=3)
                    dynamic_start = dynamic_start.replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    if not customer["ofc_start"] or customer["ofc_start"] < dynamic_start:
                        customer["ofc_start"] = dynamic_start
                    if not customer["consular_start"] or customer["consular_start"] < dynamic_start:
                        customer["consular_start"] = dynamic_start

                if action_mode == "RESCHEDULE_CONSULAR":
                    # ── Consular Reschedule Only path ────────────────────────────
                    consular_slot, matched_consular_city = find_valid_consular_slot(
                        consular_buckets,
                        customer["consular_cities"],
                        customer["consular_start"],
                        customer["consular_end"],
                    )

                    if not consular_slot:
                        continue

                    alert_key = make_alert_key(
                        customer_name, "", matched_consular_city,
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

                    state_file = Path(__file__).parent / f"state_{customer_name}.json"
                    bot_state = _read_bot_state(state_file)

                    if bot_state.get("extension_running"):
                        print(f"⏭️  Extension already running for '{customer_name}' — skipping.")
                        continue

                    bot_state.update({
                        "extension_running": False,
                        "pending": True,
                        "action_type": "RESCHEDULE_CONSULAR",
                        "consularCities": customer["consular_cities"],
                        "consularPriorityCity": matched_consular_city,
                        "consularStartDate": customer["consular_start"].strftime("%Y-%m-%d"),
                        "consularEndDate": customer["consular_end"].strftime("%Y-%m-%d"),
                        "customer_name": customer_name,
                    })
                    _write_bot_state(state_file, bot_state)
                    print(f"✅ Reschedule trigger queued for '{customer_name}'.")

                else:
                    # ── Full Booking (SNIPER) path ─────────────────────────────────
                    valid_pair, _, _, matched_ofc_city, matched_consular_city = find_valid_pair(
                        ofc_buckets,
                        consular_buckets,
                        customer["ofc_cities"],
                        customer["consular_cities"],
                        customer["ofc_start"],
                        customer["ofc_end"],
                        customer["consular_start"],
                        customer["consular_end"],
                    )

                    if not valid_pair:
                        continue

                    ofc, consular = valid_pair

                    alert_key = make_alert_key(
                        customer_name,
                        matched_ofc_city,
                        matched_consular_city,
                        ofc["display_date"],
                        consular["display_date"],
                    )

                    if not should_alert(alert_key, state):
                        continue

                    send_slack(
                        format_slack_message(
                            customer,
                            ofc,
                            consular,
                            matched_ofc_city,
                            matched_consular_city,
                        )
                    )
                    mark_alert(alert_key, state)

                    print(
                        f"✅ Alert sent for {customer_name} | "
                        f"OFC {matched_ofc_city} {ofc['display_date']} ({ofc['count']} slots) | "
                        f"Consular {matched_consular_city} {consular['display_date']} ({consular['count']} slots)"
                    )

                    # ── Signal bot2 to book immediately ────────────────────────
                    state_file = Path(__file__).parent / f"state_{customer_name}.json"

                    bot_state = _read_bot_state(state_file)

                    if bot_state.get("extension_running"):
                        print(f"⏭️  Extension already running for '{customer_name}' — skipping.")
                        continue

                    # Extension is idle — write slot data + set pending=True
                    bot_state.update({
                        "extension_running": False,
                        "pending": True,
                        "action_type": "SNIPER",
                        "ofcCities": customer["ofc_cities"],
                        "ofcPriorityCity": matched_ofc_city,
                        "ofcStartDate": customer["ofc_start"].strftime("%Y-%m-%d"),
                        "ofcEndDate": customer["ofc_end"].strftime("%Y-%m-%d"),
                        "consularCities": customer["consular_cities"],
                        "consularPriorityCity": matched_consular_city,
                        "consularStartDate": customer["consular_start"].strftime("%Y-%m-%d"),
                        "consularEndDate": customer["consular_end"].strftime("%Y-%m-%d"),
                        "customer_name": customer_name,
                    })
                    _write_bot_state(state_file, bot_state)
                    print(f"✅ Trigger queued for '{customer_name}' — booking runner will pick it up.")


            save_state(state)
        except Exception as e:
            print(f"❌ Error: {e}")
            try:
                send_slack_error(f"Error in slot monitor: {e}")
            except Exception as slack_e:
                print(f"❌ Failed to send Slack error notification: {slack_e}")
            time.sleep(ERROR_BACKOFF_SECONDS)
            continue

        time.sleep(random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS))
