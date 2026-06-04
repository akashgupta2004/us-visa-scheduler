"""
test_trigger.py — Writes a customer-specific trigger.json to test the isolated bot logic.

How to use:
  1. Start the bot for a specific customer in the GUI (e.g., CustA on port 9222).
  2. Run this script: python test_trigger.py --customer CustA --cdp-port 9222
  3. The script verifies Chrome is reachable, then drops trigger_CustA.json.
  4. Only the CustA bot will pick it up and attempt the booking.
"""

import json
import socket
import time
import argparse
from pathlib import Path

# ─── Default Config ────────────────────────────────────────────
WARMUP_WAIT  = 8   # seconds to wait after Chrome is detected (for bot to park)

def create_trigger_data(customer: str) -> dict:
    return {
        "ofc_city":        "HYDERABAD",
        "ofc_date":        "08 Oct 2026",
        "consular_city":   "HYDERABAD",
        "consular_date":   "15 Oct 2026",
        "customer_name":   customer,
    }

def chrome_is_up(port: int):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False

def main():
    parser = argparse.ArgumentParser(description="Test Script for Isolated Bots")
    parser.add_argument("--customer", type=str, default="pragadeesh28", help="Name of the customer to trigger")
    parser.add_argument("--cdp-port", type=int, default=9222, help="Chrome debug port for this customer")
    args = parser.parse_args()

    print("=" * 55)
    print(f"  Bot2 Trigger Test  --> Target: '{args.customer}'")
    print("=" * 55)

    # Step 1 — wait for Chrome CDP to be reachable
    print(f"\n[1/3] Waiting for Chrome debug port {args.cdp_port} to be active...")
    while not chrome_is_up(args.cdp_port):
        print(f"      Chrome not detected on port {args.cdp_port}, retrying in 2s...")
        time.sleep(2)
    print("      ✅ Chrome is up!")

    # Step 2 — wait for bot2 warm-up
    print(f"\n[2/3] Giving bot 8s (if just started) to warm up on the OFC page...")
    for i in range(WARMUP_WAIT, 0, -1):
        print(f"      {i}s remaining...", end="\r")
        time.sleep(1)
    print("      ✅ Warm-up wait done!       ")

    # Step 3 — write trigger file
    trigger_data = create_trigger_data(args.customer)
    trigger_file = Path(__file__).parent / f"trigger_{args.customer}.json"
    
    print(f"\n[3/3] Writing isolated trigger file...")
    print(f"      → {trigger_file.name}")
    trigger_file.write_text(json.dumps(trigger_data, indent=2), encoding="utf-8")
    print("      ✅ Trigger file written!")
    print()
    print("  The bot assigned to this customer should pick it up within 0.5s.")
    print("=" * 55)

if __name__ == "__main__":
    main()
