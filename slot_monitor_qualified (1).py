import time
import random
import json
import hashlib
import os
import sys
from pathlib import Path
from datetime import datetime

# Force UTF-8 output so emojis don't crash on Windows when piped
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import pandas as pd
import requests

URL = "https://app.checkvisaslots.com/slots/v3"
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "extversion": "4.7.0.2",
    "origin": "chrome-extension://beepaenfejnphdgnkmccjcfiieihhogl",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "x-api-key": "4XYRAN",
}

SLACK_WEBHOOK = "https://hooks.slack.com/services/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"
EXCEL_PATH = "slot_notification.csv"

REQUEST_TIMEOUT = 15
POLL_MIN_SECONDS = 15
POLL_MAX_SECONDS = 20
ALERT_COOLDOWN_SECONDS = 15 * 60
ERROR_BACKOFF_SECONDS = 25
STATE_FILE = Path("slot_alert_state.json")

CITY_ALIASES = {
    "MUMBAI": "MUMBAI",
    "MUMBAI VAC": "MUMBAI",
    "CHENNAI": "CHENNAI",
    "CHENNAI VAC": "CHENNAI",
    "HYDERABAD": "HYDERABAD",
    "HYDERABAD VAC": "HYDERABAD",
    "DELHI": "DELHI",
    "DELHI VAC": "DELHI",
    "NEW DELHI": "DELHI",
    "NEW DELHI VAC": "DELHI",
    "KOLKATA": "KOLKATA",
    "KOLKATA VAC": "KOLKATA",
}

ALL_CONSULATES = ["CHENNAI", "HYDERABAD", "MUMBAI", "DELHI", "KOLKATA"]

DATE_FORMATS = [
    "%d %b %Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
]

def normalize_city(value: str) -> str:
    raw = str(value or "").strip().upper()
    if raw == "ANY":
        return "ANY"
    return CITY_ALIASES.get(raw, raw)

def parse_date(value):
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value

    try:
        if hasattr(value, "to_pydatetime"):
            return value.to_pydatetime()
    except Exception:
        pass

    s = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    try:
        return pd.to_datetime(s).to_pydatetime()
    except Exception:
        return None

def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default

def is_ofc_location(visa_location: str) -> bool:
    return "VAC" in str(visa_location or "").upper()

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

def send_slack(msg):
    payload = {
        "text": f"🎯 *Qualified slot match found*\n{msg}"
    }
    response = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    response.raise_for_status()

def load_customers():
    if EXCEL_PATH.lower().endswith(".csv"):
        df = pd.read_csv(EXCEL_PATH)
    else:
        df = pd.read_excel(EXCEL_PATH)

    required_cols = [
        "customer_name",
        "ofc_location",
        "consular_location",
        "need_before",
        "min_slots",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in Excel: {missing}")

    customers = []
    for _, row in df.iterrows():
        customer_name = str(row.get("customer_name", "")).strip()
        if not customer_name:
            continue

        need_before = parse_date(row.get("need_before"))
        if not need_before:
            raise ValueError(f"Invalid need_before for customer: {customer_name}")

        customers.append({
            "customer_name": customer_name,
            "ofc_location": normalize_city(row.get("ofc_location")),
            "consular_location": normalize_city(row.get("consular_location")),
            "need_before": need_before,
            "min_slots": max(1, safe_int(row.get("min_slots"), 1)),
        })

    return customers

def fetch_rows():
    response = requests.get(URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)

    if response.status_code != 200:
        print(f"⚠️ HTTP {response.status_code}: {response.text[:200]}")
        return []

    text = response.text.strip()
    if not text:
        print("⚠️ Empty response body")
        return []

    try:
        data = response.json()
    except Exception:
        print(f"⚠️ Non-JSON response: {text[:300]}")
        return []

    slot_details = data.get("slotDetails", [])
    if not isinstance(slot_details, list):
        return []

    return slot_details

def build_buckets(rows):
    ofc_buckets = {}
    consular_buckets = {}

    for row in rows:
        loc_raw = row.get("visa_location", "")
        city = normalize_city(loc_raw)
        count = safe_int(row.get("slots"), 0)
        display_date = row.get("start_date", "")
        date_obj = parse_date(display_date)

        if not city or count < 1 or not date_obj:
            continue

        item = {
            "date": date_obj,
            "display_date": str(display_date),
            "count": count,
            "city": city,
        }

        if is_ofc_location(loc_raw):
            ofc_buckets.setdefault(city, []).append(item)
        else:
            consular_buckets.setdefault(city, []).append(item)

    for bucket in (ofc_buckets, consular_buckets):
        for city in bucket:
            bucket[city].sort(key=lambda x: x["date"])

    return ofc_buckets, consular_buckets

def eligible_rows(rows, need_before, min_slots):
    return [
        row for row in rows
        if row["date"] <= need_before and row["count"] >= min_slots
    ]

def get_candidate_cities(requested_city):
    if requested_city == "ANY":
        return ALL_CONSULATES
    return [requested_city]

def find_valid_pair(ofc_buckets, consular_buckets, ofc_city_pref, consular_city_pref, need_before, min_slots):
    ofc_candidate_cities = get_candidate_cities(ofc_city_pref)
    consular_candidate_cities = get_candidate_cities(consular_city_pref)

    best_pair = None
    best_ofc_matches = []
    best_consular_matches = []
    best_ofc_city = None
    best_consular_city = None

    for ofc_city in ofc_candidate_cities:
        ofc_rows = eligible_rows(ofc_buckets.get(ofc_city, []), need_before, min_slots)

        if not ofc_rows:
            continue

        for consular_city in consular_candidate_cities:
            consular_rows = eligible_rows(consular_buckets.get(consular_city, []), need_before, min_slots)

            if not consular_rows:
                continue

            for ofc in ofc_rows:
                for consular in consular_rows:
                    if ofc["date"] <= consular["date"]:
                        if best_pair is None:
                            best_pair = (ofc, consular)
                            best_ofc_matches = ofc_rows
                            best_consular_matches = consular_rows
                            best_ofc_city = ofc_city
                            best_consular_city = consular_city
                        else:
                            current_ofc, current_consular = best_pair
                            if (
                                consular["date"] < current_consular["date"]
                                or (
                                    consular["date"] == current_consular["date"]
                                    and ofc["date"] < current_ofc["date"]
                                )
                            ):
                                best_pair = (ofc, consular)
                                best_ofc_matches = ofc_rows
                                best_consular_matches = consular_rows
                                best_ofc_city = ofc_city
                                best_consular_city = consular_city

    return best_pair, best_ofc_matches, best_consular_matches, best_ofc_city, best_consular_city

def format_slack_message(customer, chosen_ofc, chosen_consular, matched_ofc_city, matched_consular_city):
    return (
        f"*Customer:* {customer['customer_name']}\n"
        f"*Requested OFC:* {customer['ofc_location']}\n"
        f"*Requested Consular:* {customer['consular_location']}\n"
        f"*Matched OFC City:* {matched_ofc_city}\n"
        f"*Matched Consular City:* {matched_consular_city}\n"
        f"*Need before:* {customer['need_before'].strftime('%Y-%m-%d')}\n\n"
        f"*OFC Date:* {chosen_ofc['display_date']}\n"
        f"*OFC Slots Available:* {chosen_ofc['count']}\n\n"
        f"*Consular Date:* {chosen_consular['display_date']}\n"
        f"*Consular Slots Available:* {chosen_consular['count']}"
    )

print("Running qualified slot monitor...")
state = load_state()
customers = load_customers()

while True:
    try:
        rows = fetch_rows()
        print(f"[{time.strftime('%H:%M:%S')}] rows fetched: {len(rows)}")

        if not rows:
            time.sleep(random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS))
            continue

        ofc_buckets, consular_buckets = build_buckets(rows)
        sent_count = 0

        for customer in customers:
            valid_pair, _, _, matched_ofc_city, matched_consular_city = find_valid_pair(
                ofc_buckets,
                consular_buckets,
                customer["ofc_location"],
                customer["consular_location"],
                customer["need_before"],
                customer["min_slots"]
            )

            if not valid_pair:
                continue

            ofc, consular = valid_pair

            alert_key = make_alert_key(
                customer["customer_name"],
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
                f"✅ Alert sent for {customer['customer_name']} | "
                f"OFC {matched_ofc_city} {ofc['display_date']} ({ofc['count']} slots) | "
                f"Consular {matched_consular_city} {consular['display_date']} ({consular['count']} slots)"
            )
            sent_count += 1

            # ── Signal bot2 to book immediately ──────────────────────
            # Isolated trigger file per customer
            trigger_filename = f"trigger_{customer['customer_name']}.json"
            trigger_path = Path(os.path.dirname(os.path.abspath(__file__))) / trigger_filename
            trigger_data = {
                "ofc_city": matched_ofc_city,
                "ofc_date": str(ofc["display_date"]),
                "consular_city": matched_consular_city,
                "consular_date": str(consular["display_date"]),
                "customer_name": customer["customer_name"],
            }
            try:
                trigger_path.write_text(json.dumps(trigger_data, indent=2), encoding="utf-8")
                print(f"📥 {trigger_filename} written → bot2 will now book the slot.")
            except Exception as ex:
                print(f"❌ Failed to write {trigger_filename}: {ex}")

        save_state(state)
        print(f"[{time.strftime('%H:%M:%S')}] alerts sent: {sent_count}")

    except Exception as e:
        print(f"❌ Error: {e}")
        time.sleep(ERROR_BACKOFF_SECONDS)

    time.sleep(random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS))

