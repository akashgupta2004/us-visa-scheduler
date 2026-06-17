import os
import csv
import time
from datetime import datetime
from pathlib import Path
from src.monitor.matcher import safe_int

SLOTS_ANALYSIS_FILE = Path(__file__).parent.parent.parent / "slots_data_analysis.csv"

def log_slots_for_analysis(rows):
    if not rows:
        return 0
        
    file_exists = os.path.isfile(SLOTS_ANALYSIS_FILE)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logged_count = 0
    
    try:
        with open(SLOTS_ANALYSIS_FILE, 'a', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['timestamp', 'appointment_type', 'visa_location', 'start_date', 'slots']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
                
            for row in rows:
                slots = safe_int(row.get("slots"), 0)
                loc = row.get("visa_location", "")
                appt_type = "OFC" if "VAC" in str(loc).upper() else "Consular"
                
                if slots > 0:
                    writer.writerow({
                        'timestamp': timestamp,
                        'appointment_type': appt_type,
                        'visa_location': loc,
                        'start_date': row.get("start_date", ""),
                        'slots': slots
                    })
                    logged_count += 1
                else:
                    print(f"[{timestamp}] 0 slots for {appt_type} at {loc}")
    except Exception as e:
        print(f"File write error: {e}")
        
    return logged_count



