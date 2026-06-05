import requests
from typing import List, Dict

URL = "https://app.checkvisaslots.com/slots/v3"
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "extversion": "4.7.0.2",
    "origin": "chrome-extension://beepaenfejnphdgnkmccjcfiieihhogl",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "x-api-key": "4XYRAN",
}
REQUEST_TIMEOUT = 15

def fetch_rows() -> List[Dict]:
    try:
        response = requests.get(URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            print(f"⚠️ HTTP {response.status_code}: {response.text[:200]}")
            return []

        text = response.text.strip()
        if not text:
            print("⚠️ Empty response body")
            return []

        data = response.json()
        slot_details = data.get("slotDetails", [])
        if not isinstance(slot_details, list):
            return []

        return slot_details
    except Exception as e:
        print(f"⚠️ API Error: {e}")
        return []
