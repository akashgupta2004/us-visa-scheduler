import time
import json
import os
import re
from curl_cffi import requests as cffi_requests
import requests as std_requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

URL = "https://app.checkvisaslots.com/slots/v3"
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "extversion": "4.7.0",
    "origin": "chrome-extension://beepaenfejnphdgnkmccjcfiieihhogl",
    "x-api-key": "4XYRAN",
}

_cvs_api_keys = [k.strip() for k in os.getenv("CVS_API_KEYS", "4XYRAN").split(",") if k.strip()]
_current_key_idx = 0

def get_next_api_key() -> str:
    global _current_key_idx
    if not _cvs_api_keys:
        return "4XYRAN"
    key = _cvs_api_keys[_current_key_idx % len(_cvs_api_keys)]
    _current_key_idx += 1
    return key

def fetch_rows():
    try:
        HEADERS["x-api-key"] = get_next_api_key()
        response = cffi_requests.get(URL, headers=HEADERS, timeout=15, impersonate="chrome120")
        
        if response.status_code == 429:
            print("⚠️ CheckVisaSlots Quota Exhausted! (HTTP 429)")
            return []
        elif response.status_code == 202:
            print("⚠️ HTTP 202 Response. Likely AWS WAF Challenge. Ignoring...")
            return []
        elif response.status_code != 200:
            print(f"⚠️ HTTP {response.status_code} Response: {response.text[:200]}")
            return []

        text = response.text.strip()
        if not text:
            return []

        data = response.json()
        slot_details = data.get("slotDetails", [])
        if not isinstance(slot_details, list):
            return []

        return slot_details
    except Exception as e:
        print(f"⚠️ API Error: {e}")
        return []

def normalize_city(city_str):
    if not city_str:
        return ""
    return str(city_str).strip().upper()

def send_slack(message):
    if not SLACK_WEBHOOK:
        print("⚠️ SLACK_WEBHOOK_URL not set in .env")
        return False
    payload = {"text": f"{message}"}
    try:
        r = std_requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ Failed to send Slack message: {e}")
        return False

def main():
    print("Starting Independent CVS Slack Poller...")
    print("Will poll CheckVisaSlots and send updates to Slack.")
    
    last_slots_json = ""
    
    while True:
        try:
            print("\nFetching slots from CheckVisaSlots...")
            rows = fetch_rows()
            
            if not rows:
                print("No rows returned from API. Waiting 60 seconds...")
                time.sleep(60)
                continue
                
            filtered_rows = []
            for row in rows:
                loc = row.get("visa_location", "")
                date = row.get("start_date", "")
                count = row.get("slots", 0)
                
                if loc and count and int(count) > 0:
                    filtered_rows.append({
                        "location": loc,
                        "date": date,
                        "count": int(count)
                    })
                    
            filtered_rows.sort(key=lambda x: (x["location"], x["date"]))
            current_slots = json.dumps(filtered_rows)
            
            if current_slots != last_slots_json:
                print(f"New slot data found! ({len(filtered_rows)} entries). Sending to Slack...")
                
                if not filtered_rows:
                    msg = "ℹ️ *CheckVisaSlots Update*\nNo slots currently available anywhere."
                else:
                    msg = "🔍 *CheckVisaSlots Update*\n"
                    by_city = {}
                    for r in filtered_rows:
                        city = normalize_city(r["location"])
                        is_ofc = "VAC" in r["location"].upper()
                        loc_type = "OFC" if is_ofc else "Consular"
                        
                        group_key = f"{city} ({loc_type})"
                        if group_key not in by_city:
                            by_city[group_key] = []
                        by_city[group_key].append(f"{r['date']} - {r['count']} slots")
                        
                    for city_str, dates in by_city.items():
                        msg += f"\n*{city_str}*\n"
                        for d in dates:
                            msg += f"• {d}\n"
                
                send_slack(msg)
                last_slots_json = current_slots
            else:
                print("Slot data unchanged from previous poll.")
                
            time.sleep(60)
            
        except Exception as e:
            print(f"⚠️ Error polling CVS: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
