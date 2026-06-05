import os
import csv
import time
from datetime import datetime
from pathlib import Path
from src.monitor.matcher import safe_int
from slack import send as slack_send

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
                if slots > 0:
                    loc = row.get("visa_location", "")
                    appt_type = "OFC" if "VAC" in str(loc).upper() else "Consular"
                    
                    writer.writerow({
                        'timestamp': timestamp,
                        'appointment_type': appt_type,
                        'visa_location': loc,
                        'start_date': row.get("start_date", ""),
                        'slots': slots
                    })
                    logged_count += 1
    except Exception as e:
        print(f"File write error: {e}")
        
    return logged_count


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


def send_slack(msg):
    slack_send(f"🎯 *Qualified slot match found*\n{msg}")
