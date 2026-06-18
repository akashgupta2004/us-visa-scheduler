import requests
from typing import List, Dict

URL = "https://app.checkvisaslots.com/slots/v3"
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "extversion": "4.7.2",
    "origin": "chrome-extension://beepaenfejnphdgnkmccjcfiieihhogl",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "x-api-key": "9EHYC6",
}
REQUEST_TIMEOUT = 15

def fetch_rows() -> List[Dict]:
    try:
        response = requests.get(URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            print(f"⚠️ HTTP {response.status_code} Response:")
            print(f"Headers: {dict(response.headers)}")
            print(f"Body: {response.text[:500]}")
            return []

        text = response.text.strip()
        if not text:
            print(f"⚠️ Empty response body (HTTP {response.status_code})")
            print(f"Headers: {dict(response.headers)}")
            return []

        data = response.json()
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
        return []
