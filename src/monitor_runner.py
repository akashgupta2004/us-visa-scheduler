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
import os
import hashlib
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Force UTF-8 output so emojis don't crash on Windows when piped utf 16
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
from src.common.state import (
    read_state as _read_bot_state,
    try_queue_local_trigger,
)
from slack import format_slack_message, send_slack, send_slack_error

ALERT_COOLDOWN_SECONDS = 15 * 60
TRIGGER_COOLDOWN_SECONDS = int(
    os.getenv("REMOTE_TRIGGER_COOLDOWN_SECONDS", "300")
)
ERROR_BACKOFF_SECONDS = 40

POLL_MIN_SECONDS = 20
POLL_MAX_SECONDS = 25
RESERVED_TRIGGER_STAGGER_SECONDS = float(
    os.getenv("RESERVED_TRIGGER_STAGGER_SECONDS", "1.0")
)

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


def _send_alert_if_due(alert_key, state, message, success_message):
    """Send Slack only when due. Slack must never block a booking trigger."""
    if not should_alert(alert_key, state):
        return False

    try:
        sent = send_slack(message)
    except Exception as error:
        print(
            f"⚠️ Slack alert failed, but booking trigger continues: {error}"
        )
        return False

    if sent:
        mark_alert(alert_key, state)
        print(success_message)
        return True

    print("⚠️ Slack alert was not sent, but booking trigger continues.")
    return False


def _write_trigger_if_idle(
    state_file,
    customer_name,
    trigger_updates,
    current_triggers,
    max_triggers,
):
    """
    Queue a CVS trigger only when the account is eligible.

    max_triggers=0 means unlimited.
    """
    if max_triggers > 0 and current_triggers >= max_triggers:
        print(
            f"⏭️ Skipping {customer_name}: maximum CVS triggers "
            f"({max_triggers}) reached for this cycle."
        )
        return current_triggers, False

    queued, reason = try_queue_local_trigger(
        state_file,
        trigger_updates,
    )

    if not queued:
        if reason == "completed":
            print(
                f"⏭️ Skipping {customer_name}: booking already completed."
            )
        elif reason == "running":
            print(
                f"⏭️ Skipping {customer_name}: booking is already running."
            )
        elif reason == "pending":
            print(
                f"⏭️ Skipping {customer_name}: another trigger is pending."
            )
        elif reason.startswith("resting:"):
            try:
                remaining = int(reason.split(":", 1)[1])
            except Exception:
                remaining = 0
            print(
                f"💤 Skipping {customer_name}: booking rest active "
                f"for another {remaining} second(s)."
            )
        elif reason == "lock_timeout":
            print(
                f"⚠️ Skipping {customer_name}: could not safely lock "
                f"the state file."
            )
        else:
            print(
                f"⏭️ Skipping {customer_name}: trigger not queued "
                f"({reason})."
            )

        return current_triggers, False

    print(f"✅ CVS trigger queued for '{customer_name}'.")
    return current_triggers + 1, True

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
            "prevent_immediate": entry.get("prevent_immediate", False),
            "multiPerson": entry.get("multiPerson", False),
            "role": entry.get("role", "POLLING_ONLY")
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
    print(
        f"Running qualified slot monitor "
        f"(interval ~{POLL_MIN_SECONDS}s, unlimited fetches)..."
    )
    state = load_state()

    while True:
        # Reload customers on every iteration to pick up GUI changes.
        customers = load_customers()

        try:
            rows = fetch_rows()
            if not rows:
                print(
                    "⚠️ API returned empty slot data (no rows). "
                    "Monitor is still running, waiting for next poll..."
                )
                time.sleep(
                    random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS)
                )
                continue

            log_slots_for_analysis(rows)
            ofc_buckets, consular_buckets = build_buckets(rows)
            # Zero means every matching eligible account can trigger.
            max_triggers = int(
                os.getenv("MAX_MONITOR_TRIGGERS", "0")
            )
            current_triggers = 0
            alert_jobs = []

            for customer in customers:
                customer_name = customer["customer_name"]
                action_mode = customer["action_mode"]
                uid = customer["uid"]
                role = str(
                    customer.get("role", "")
                ).strip().upper()
                state_file = (
                    Path(__file__).parent / f"state_{uid}.json"
                )
                bot_state = _read_bot_state(state_file)

                if bot_state.get("completed"):
                    continue

                effective_ofc_start, effective_consular_start = (
                    _get_effective_dates(customer)
                )

                if bot_state.get("waitingForConsular"):
                    # ── Fallback Consular-Only path (Post-OFC) ───────────────
                    booked_ofc_date_str = bot_state.get("bookedOfcDate")
                    if booked_ofc_date_str:
                        booked_date_obj = parse_date(
                            booked_ofc_date_str
                        )
                        if booked_date_obj:
                            minimum_consular_date = (
                                booked_date_obj + timedelta(days=1)
                            )
                            if effective_consular_start:
                                effective_consular_start = max(
                                    effective_consular_start,
                                    minimum_consular_date,
                                )
                            else:
                                effective_consular_start = (
                                    minimum_consular_date
                                )

                    (
                        consular_slot,
                        matched_consular_city,
                    ) = find_valid_consular_slot(
                        consular_buckets,
                        customer["consular_cities"],
                        effective_consular_start,
                        customer["consular_end"],
                    )

                    if not consular_slot:
                        continue

                    alert_key = make_alert_key(
                        uid,
                        "",
                        matched_consular_city,
                        "",
                        consular_slot["display_date"],
                    )

                    action_type = (
                        "RESCHEDULE_FULL_CONSULAR_ONLY"
                        if action_mode == "RESCHEDULE_FULL"
                        else "SNIPER_CONSULAR_ONLY"
                    )

                    trigger_updates = {
                        "extension_running": False,
                        "pending": True,
                        "trigger_timestamp": time.time(),
                        "trigger_key": alert_key,
                        "action_type": action_type,
                        "consularCities": customer[
                            "consular_cities"
                        ],
                        "consularPriorityCity": (
                            matched_consular_city
                        ),
                        "consularStartDate": (
                            effective_consular_start.strftime(
                                "%Y-%m-%d"
                            )
                            if effective_consular_start
                            else ""
                        ),
                        "consularEndDate": (
                            customer["consular_end"].strftime(
                                "%Y-%m-%d"
                            )
                            if customer["consular_end"]
                            else ""
                        ),
                        "customer_name": customer_name,
                        "prevent_immediate": customer.get(
                            "prevent_immediate", False
                        ),
                        "multiPerson": customer.get(
                            "multiPerson", False
                        ),
                    }

                    # Queue first. Slack must never delay the booking trigger.
                    current_triggers, queued = _write_trigger_if_idle(
                        state_file,
                        customer_name,
                        trigger_updates,
                        current_triggers,
                        max_triggers,
                    )

                    if queued:
                        alert_jobs.append(
                            (
                                alert_key,
                                format_slack_message(
                                    customer,
                                    None,
                                    consular_slot,
                                    None,
                                    matched_consular_city,
                                ),
                                (
                                    f"✅ [FALLBACK] Alert sent for "
                                    f"{customer_name} | Consular "
                                    f"{matched_consular_city} "
                                    f"{consular_slot['display_date']} "
                                    f"({consular_slot['count']} slots)"
                                ),
                            )
                        )
                    continue

                if action_mode == "RESCHEDULE_CONSULAR":
                    # ── Consular Reschedule Only path ───────────────────────
                    (
                        consular_slot,
                        matched_consular_city,
                    ) = find_valid_consular_slot(
                        consular_buckets,
                        customer["consular_cities"],
                        effective_consular_start,
                        customer["consular_end"],
                    )

                    if not consular_slot:
                        continue

                    alert_key = make_alert_key(
                        uid,
                        "",
                        matched_consular_city,
                        "",
                        consular_slot["display_date"],
                    )

                    trigger_updates = {
                        "extension_running": False,
                        "pending": True,
                        "trigger_timestamp": time.time(),
                        "trigger_key": alert_key,
                        "action_type": "RESCHEDULE_CONSULAR",
                        "consularCities": customer[
                            "consular_cities"
                        ],
                        "consularPriorityCity": (
                            matched_consular_city
                        ),
                        "consularStartDate": (
                            effective_consular_start.strftime(
                                "%Y-%m-%d"
                            )
                            if effective_consular_start
                            else ""
                        ),
                        "consularEndDate": (
                            customer["consular_end"].strftime(
                                "%Y-%m-%d"
                            )
                            if customer["consular_end"]
                            else ""
                        ),
                        "customer_name": customer_name,
                        "prevent_immediate": customer.get(
                            "prevent_immediate", False
                        ),
                        "multiPerson": customer.get(
                            "multiPerson", False
                        ),
                    }

                    current_triggers, queued = _write_trigger_if_idle(
                        state_file,
                        customer_name,
                        trigger_updates,
                        current_triggers,
                        max_triggers,
                    )

                    if queued:
                        alert_jobs.append(
                            (
                                alert_key,
                                format_slack_message(
                                    customer,
                                    None,
                                    consular_slot,
                                    None,
                                    matched_consular_city,
                                ),
                                (
                                    f"✅ [RESCHEDULE] Alert sent for "
                                    f"{customer_name} | Consular "
                                    f"{matched_consular_city} "
                                    f"{consular_slot['display_date']} "
                                    f"({consular_slot['count']} slots)"
                                ),
                            )
                        )
                    continue

                # ── Full Booking (SNIPER / RESCHEDULE_FULL) path ─────────────
                ofc, matched_ofc_city = find_valid_ofc_slot(
                    ofc_buckets,
                    customer["ofc_cities"],
                    effective_ofc_start,
                    customer["ofc_end"],
                )

                if not ofc:
                    continue

                minimum_consular_date = ofc["date"] + timedelta(
                    days=1
                )
                if effective_consular_start:
                    consular_min_date = max(
                        effective_consular_start,
                        minimum_consular_date,
                    )
                else:
                    consular_min_date = minimum_consular_date

                (
                    consular,
                    matched_consular_city,
                ) = find_valid_consular_slot(
                    consular_buckets,
                    customer["consular_cities"],
                    consular_min_date,
                    customer["consular_end"],
                )

                action_type = (
                    "RESCHEDULE_FULL"
                    if action_mode == "RESCHEDULE_FULL"
                    else "SNIPER"
                )

                if consular:
                    consular_desc = (
                        f"{matched_consular_city} "
                        f"{consular['display_date']} "
                        f"({consular['count']} slots)"
                    )
                else:
                    consular_desc = "pending (wait mode)"
                    matched_consular_city = (
                        customer["consular_cities"][0]
                        if customer["consular_cities"]
                        else ""
                    )

                alert_key = make_alert_key(
                    uid,
                    matched_ofc_city,
                    "",
                    ofc["display_date"],
                    "",
                )

                trigger_updates = {
                    "extension_running": False,
                    "pending": True,
                    "trigger_timestamp": time.time(),
                    "trigger_key": alert_key,
                    "action_type": action_type,
                    "ofcCities": customer["ofc_cities"],
                    "ofcPriorityCity": matched_ofc_city,
                    "ofcStartDate": (
                        effective_ofc_start.strftime("%Y-%m-%d")
                        if effective_ofc_start
                        else ""
                    ),
                    "ofcEndDate": (
                        customer["ofc_end"].strftime("%Y-%m-%d")
                        if customer["ofc_end"]
                        else ""
                    ),
                    "consularCities": customer[
                        "consular_cities"
                    ],
                    "consularPriorityCity": (
                        matched_consular_city
                    ),
                    "consularStartDate": (
                        effective_consular_start.strftime(
                            "%Y-%m-%d"
                        )
                        if effective_consular_start
                        else ""
                    ),
                    "consularEndDate": (
                        customer["consular_end"].strftime(
                            "%Y-%m-%d"
                        )
                        if customer["consular_end"]
                        else ""
                    ),
                    "customer_name": customer_name,
                    "prevent_immediate": customer.get(
                        "prevent_immediate", False
                    ),
                    "multiPerson": customer.get(
                        "multiPerson", False
                    ),
                }

                current_triggers, queued = _write_trigger_if_idle(
                    state_file,
                    customer_name,
                    trigger_updates,
                    current_triggers,
                    max_triggers,
                )

                if queued:
                    alert_jobs.append(
                        (
                            alert_key,
                            format_slack_message(
                                customer,
                                ofc,
                                consular,
                                matched_ofc_city,
                                (
                                    matched_consular_city
                                    if consular
                                    else None
                                ),
                            ),
                            (
                                f"✅ Alert sent for {customer_name} | "
                                f"OFC {matched_ofc_city} "
                                f"{ofc['display_date']} "
                                f"({ofc['count']} slots) | "
                                f"Consular {consular_desc}"
                            ),
                        )
                    )

            # Every matching account is queued before Slack is called.
            for alert_key, message, success_message in alert_jobs:
                _send_alert_if_due(
                    alert_key,
                    state,
                    message,
                    success_message,
                )

            # Persist Slack cooldown timestamps after sending alerts.
            save_state(state)

        except Exception as e:
            print(f"❌ Error: {e}")
            last_err_time = state.get(
                "last_slack_error_time", 0
            )

            if (
                time.time() - last_err_time
                > ALERT_COOLDOWN_SECONDS
            ):
                try:
                    err_str = str(e).lower()
                    if "curl: (28)" not in err_str and "timed out" not in err_str and "timeout" not in err_str:
                        sent = send_slack_error(f"Error in slot monitor: {e}")
                        if sent:
                            state["last_slack_error_time"] = time.time()
                            save_state(state)
                except Exception as slack_e:
                    print(f"❌ Failed to send Slack error notification: {slack_e}")
            else:
                print(
                    "⏳ Slack error skipped "
                    "(cooldown active)."
                )

            time.sleep(ERROR_BACKOFF_SECONDS)
            continue

        time.sleep(
            random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS)
        )


if __name__ == "__main__":
    main()