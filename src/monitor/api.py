from curl_cffi import requests
    
from typing import List, Dict
import time
import sys
from pathlib import Path

# Add root to sys.path to import slack
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
try:
    from slack import send_slack_error
except ImportError:
    send_slack_error = None

URL = "https://app.checkvisaslots.com/slots/v3"
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "extversion": "4.7.2",
    "origin": "chrome-extension://beepaenfejnphdgnkmccjcfiieihhogl",
    "x-api-key": "9EHYC6",
}
# 4XYRAN
# 9EHYC6
REQUEST_TIMEOUT = 15

def fetch_rows() -> List[Dict]:
    try:
        # impersonate="chrome110" helps bypass AWS WAF challenges (HTTP 202)
        response = requests.get(URL, headers=HEADERS, timeout=REQUEST_TIMEOUT, impersonate="chrome110")

        if response.status_code == 429:
            raise Exception("CheckVisaSlots Quota Exhausted! (Daily limit reached - HTTP 429)")
        elif response.status_code != 200:
            err_msg = f"HTTP {response.status_code} Response: {response.text[:200]}"
            print(f"⚠️ {err_msg}")
            raise Exception(err_msg)

        text = response.text.strip()
        if not text:
            print(f"⚠️ Empty response body (HTTP {response.status_code})")
            print(f"Headers: {dict(response.headers)}")
            return []

        data = response.json()
        
        # Check for CheckVisaSlots API Quota exhaustion
        user_activity = data.get("userActivity", {})
        print(f"Quota details(remaining API fetches on this key): {user_activity.get('remaining', 'N/A')}")
        
        if "remaining" in user_activity and user_activity["remaining"] <= 500:
            remaining_fetches = user_activity["remaining"]
            global _last_quota_alert
            if '_last_quota_alert' not in globals():
                _last_quota_alert = 0
                
            if time.time() - _last_quota_alert > 900: # 15 minutes cooldown
                msg = f"⚠️ CheckVisaSlots Quota Low! (Only {remaining_fetches} API fetches remaining on this key)"
                print(msg)
                _last_quota_alert = time.time()
        slot_details = data.get("slotDetails", [])
        if not slot_details:
            print(f"⚠️ API returned 200 OK but no slotDetails found.")
            print(f"Raw JSON response: {data}")
            return []
        
        if not isinstance(slot_details, list):
            print(f"⚠️ API returned slotDetails that is not a list! Type: {type(slot_details)}")
            return []

        return slot_details
    except Exception as e:
        print(f"⚠️ API Error: {e}")
        raise e
