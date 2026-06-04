import requests
import pandas as pd

URL = "https://app.checkvisaslots.com/slots/v3"
HEADERS = {
    "accept": "*/*",
    "extversion": "4.7.0.2",
    "origin": "chrome-extension://beepaenfejnphdgnkmccjcfiieihhogl",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "x-api-key": "4XYRAN",
}

def show_available_slots():
    print("Fetching available slots...")
    try:
        response = requests.get(URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        slot_details = data.get("slotDetails", [])
        
        if not slot_details:
            print("No slots currently available (API returned an empty list).")
            return
            
        print(f"\n--- Found {len(slot_details)} Available Slots ---\n")
        
        # Convert to a pandas dataframe and sort by location
        df = pd.DataFrame(slot_details)
        
        if "visa_location" in df.columns and "start_date" in df.columns and "slots" in df.columns:
            # Sort the output so it's easy to read
            df = df.sort_values(by=["visa_location", "start_date"])
            
            # Print a neat table containing location, date, and slots available
            print(df[["visa_location", "start_date", "slots"]].to_string(index=False))
        else:
            # Fallback if the API response model changed unexpectedly
            for slot in slot_details:
                print(slot)
                
    except Exception as e:
        print(f"Error fetching slots: {e}")

if __name__ == "__main__":
    show_available_slots()
