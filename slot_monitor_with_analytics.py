import time
import random
import csv
import threading
import sys
import os
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext
import requests

# Force UTF-8 output
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

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
POLL_MIN_SECONDS = 15
POLL_MAX_SECONDS = 20
ERROR_BACKOFF_SECONDS = 25

# Data analysis file
SLOTS_ANALYSIS_FILE = "slots_data_analysis.csv"

def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default

def fetch_rows():
    """Fetch slot details from the API."""
    response = requests.get(URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        return [], f"⚠️ HTTP {response.status_code}"
    
    text = response.text.strip()
    if not text:
        return [], "⚠️ Empty response body"
    
    try:
        data = response.json()
    except Exception:
        return [], "⚠️ Non-JSON response"
    
    slot_details = data.get("slotDetails", [])
    if not isinstance(slot_details, list):
        return [], "⚠️ Invalid slot details format"
        
    return slot_details, None

def is_ofc_location(visa_location: str) -> bool:
    return "VAC" in str(visa_location or "").upper()

def log_slots_for_analysis(rows):
    """Logs the raw available slots into a CSV file."""
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
                    appt_type = "OFC" if is_ofc_location(loc) else "Consular"
                    
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

# --- GUI Application ---

BG = "#1e1e2e"
SURFACE = "#2a2a3e"
ACCENT = "#7c3aed"
SUCCESS = "#22c55e"
DANGER = "#ef4444"
WARNING = "#f59e0b"
TEXT = "#e2e8f0"
SUBTEXT = "#94a3b8"

class AnalyticsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Standalone Raw Slot Logger")
        self.geometry("700x500")
        self.configure(bg=BG)
        
        self.running = False
        self.thread = None
        
        self.build_ui()
        self.log_message("System initialized. Ready to start data collection.", "SUCCESS")
        self.log_message(f"Data will be saved to: {os.path.abspath(SLOTS_ANALYSIS_FILE)}", "INFO")

    def build_ui(self):
        header = tk.Frame(self, bg=SURFACE, pady=15, padx=20)
        header.pack(fill="x")
        
        tk.Label(header, text="Standalone Raw Slot Logger", font=("Segoe UI", 16, "bold"), fg=TEXT, bg=SURFACE).pack(side="left")
        
        self.status_lbl = tk.Label(header, text="● STOPPED", font=("Segoe UI", 10, "bold"), fg=DANGER, bg=SURFACE)
        self.status_lbl.pack(side="right", padx=10)

        controls = tk.Frame(self, bg=BG, pady=15, padx=20)
        controls.pack(fill="x")
        
        self.start_btn = tk.Button(controls, text="▶ Start Logging", command=self.start_logging, bg=SUCCESS, fg="white", font=("Segoe UI", 10, "bold"), relief="flat", padx=15, pady=8, cursor="hand2")
        self.start_btn.pack(side="left", padx=(0, 10))
        
        self.stop_btn = tk.Button(controls, text="⏹ Stop Logging", command=self.stop_logging, bg=DANGER, fg="white", font=("Segoe UI", 10, "bold"), relief="flat", padx=15, pady=8, cursor="hand2", state="disabled")
        self.stop_btn.pack(side="left")
        
        tk.Label(controls, text="Independent script: Grabs all slots, no filters.", font=("Segoe UI", 9, "italic"), fg=SUBTEXT, bg=BG).pack(side="right")

        log_frame = tk.Frame(self, bg=BG, padx=20, pady=10)
        log_frame.pack(fill="both", expand=True)
        
        self.log_area = scrolledtext.ScrolledText(log_frame, bg="#0d0d1a", fg="#a0e982", font=("Consolas", 10), relief="flat", state="disabled")
        self.log_area.pack(fill="both", expand=True)
        
        self.log_area.tag_config("SUCCESS", foreground=SUCCESS)
        self.log_area.tag_config("ERROR", foreground=DANGER)
        self.log_area.tag_config("WARN", foreground=WARNING)
        self.log_area.tag_config("INFO", foreground="#a0e982")

    def log_message(self, msg, level="INFO"):
        self.log_area.config(state="normal")
        ts = time.strftime('%H:%M:%S')
        self.log_area.insert("end", f"[{ts}] {msg}\n", level)
        self.log_area.see("end")
        self.log_area.config(state="disabled")
        self.update_idletasks()

    def start_logging(self):
        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_lbl.config(text="● RUNNING", fg=SUCCESS)
        self.log_message("Started polling API...", "INFO")
        self.thread = threading.Thread(target=self.poll_loop, daemon=True)
        self.thread.start()

    def stop_logging(self):
        self.running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_lbl.config(text="● STOPPED", fg=DANGER)
        self.log_message("Stopping polling... (Will stop after current cycle)", "WARN")

    def poll_loop(self):
        while self.running:
            try:
                rows, err = fetch_rows()
                if err:
                    self.after(0, self.log_message, f"Fetch warning: {err}", "WARN")
                else:
                    self.after(0, self.log_message, f"Fetched {len(rows)} total entries from API.", "INFO")
                    
                    logged_count = log_slots_for_analysis(rows)
                    if logged_count > 0:
                        self.after(0, self.log_message, f"✅ Logged {logged_count} available slots to CSV.", "SUCCESS")
                    else:
                        self.after(0, self.log_message, "No available slots found in this cycle.", "WARN")

            except Exception as e:
                self.after(0, self.log_message, f"Error in polling loop: {e}", "ERROR")
                time.sleep(ERROR_BACKOFF_SECONDS)
                continue

            # Sleep randomly between 15 to 20 seconds
            sleep_time = random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS)
            for _ in range(int(sleep_time)):
                if not self.running:
                    break
                time.sleep(1)

        self.after(0, self.log_message, "Polling fully stopped.", "WARN")

if __name__ == "__main__":
    app = AnalyticsApp()
    app.mainloop()
