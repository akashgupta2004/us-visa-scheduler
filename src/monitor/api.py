from curl_cffi import requests
from typing import List, Dict
import time
import re
from pathlib import Path

URL = "https://app.checkvisaslots.com/slots/v3"
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "extversion": "4.7.0",
    "origin": "chrome-extension://beepaenfejnphdgnkmccjcfiieihhogl",
    "x-api-key": "9EHYC6",
}
# 4XYRAN
# 9EHYC6
REQUEST_TIMEOUT = 15

# Tracks when we last warned about low quota (module-level, not global hack)
_last_quota_alert: float = 0.0

def fetch_rows() -> List[Dict]:
    global _last_quota_alert
    try:
        # impersonate="chrome120" helps bypass AWS WAF challenges (HTTP 202)
        response = requests.get(URL, headers=HEADERS, timeout=REQUEST_TIMEOUT, impersonate="chrome120")

        if response.status_code == 429:
            raise Exception("CheckVisaSlots Quota Exhausted! (Daily limit reached - HTTP 429)")
        elif response.status_code == 202:
            print(f"⚠️ HTTP 202 Response. Likely AWS WAF Challenge. Ignoring and retrying on next cycle.")
            return []
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
        
        # Check for outdated extension message and auto-update
        tip_msg = data.get("tipMessage", "")
        api_msg = data.get("message", "")
        
        if "outdated Check Visa Slots extension" in tip_msg or "outdated Check Visa Slots extension" in api_msg:
            target_str = api_msg if "outdated" in api_msg else tip_msg
            match = re.search(r'v(\d+\.\d+\.\d+)', target_str)
            if match:
                new_version = match.group(1)
                old_version = HEADERS["extversion"]
                if new_version != old_version:
                    print(f"🔄 Auto-updating CheckVisaSlots extension version from {old_version} to {new_version}...")
                    
                    # Update memory
                    HEADERS["extversion"] = new_version
                    
                    # Update file to persist across restarts
                    api_file = Path(__file__).resolve()
                    try:
                        content = api_file.read_text(encoding="utf-8")
                        content = re.sub(r'("extversion"\s*:\s*")[^"]+(")', f'\\g<1>{new_version}\\g<2>', content, count=1)
                        api_file.write_text(content, encoding="utf-8")
                    except Exception as e:
                        print(f"⚠️ Failed to update api.py on disk: {e}")
                        
                    # Retry the fetch immediately with the new version
                    return fetch_rows()
            else:
                print(f"⚠️ Extension is outdated but couldn't parse the new version from message: {target_str}")
        
        # Check for CheckVisaSlots API Quota exhaustion
        user_activity = data.get("userActivity", {})
        print(f"Quota details(remaining API fetches on this key): {user_activity.get('remaining', 'N/A')}")
        
        if "remaining" in user_activity and user_activity["remaining"] <= 500:
            remaining_fetches = user_activity["remaining"]
            if time.time() - _last_quota_alert > 900:  # 15 minutes cooldown
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

