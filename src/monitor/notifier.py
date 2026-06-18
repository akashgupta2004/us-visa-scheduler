import os
import csv
import json
import time
from datetime import datetime
from pathlib import Path
from src.monitor.matcher import safe_int

SLOTS_ANALYSIS_CSV = Path(__file__).parent.parent.parent / "slots_data_analysis.csv"
SLOTS_ANALYSIS_JSON = Path(__file__).parent.parent.parent / "slots_data_analysis.jsonl"

def log_slots_for_analysis(rows):
    if not rows:
        return 0
        
    csv_exists = os.path.isfile(SLOTS_ANALYSIS_CSV)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logged_count = 0
    
    try:
        with open(SLOTS_ANALYSIS_CSV, 'a', newline='', encoding='utf-8') as csvfile, \
             open(SLOTS_ANALYSIS_JSON, 'a', encoding='utf-8') as jsonfile:
             
            fieldnames = ['timestamp', 'appointment_type', 'visa_location', 'start_date', 'slots']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not csv_exists:
                writer.writeheader()
                
            for row in rows:
                slots = safe_int(row.get("slots"), 0)
                loc = row.get("visa_location", "")
                appt_type = "OFC" if "VAC" in str(loc).upper() else "Consular"
                
                if slots > 0:
                    # Write to CSV
                    writer.writerow({
                        'timestamp': timestamp,
                        'appointment_type': appt_type,
                        'visa_location': loc,
                        'start_date': row.get("start_date", ""),
                        'slots': slots
                    })
                    
                    # Write to JSON Lines
                    row_copy = dict(row)
                    row_copy["fetch_timestamp"] = timestamp
                    row_copy["appointment_type"] = appt_type
                    jsonfile.write(json.dumps(row_copy) + "\n")
                    
                    logged_count += 1
    except Exception as e:
        print(f"File write error: {e}")
        
    return logged_count
