"""
slack.py — Send a message to the VisaBot Slack channel.

Usage:
    python slack.py "Your message here"
    python slack.py  (opens interactive prompt)
"""

import os
import sys
import requests

# Force UTF-8 output so emojis don't crash on Windows when piped
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Load webhook URL from environment variable
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")


def send(message: str, emoji: str = "💬") -> bool:
    """Send a message to Slack. Returns True on success."""
    if not SLACK_WEBHOOK:
        return False
    payload = {"text": f"{emoji} {message}"}
    try:
        r = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
        print(f"✅ Sent: {message}")
        return True
    except Exception as e:
        print(f"❌ Failed to send: {e}")
        return False
def format_slack_message(customer, chosen_ofc, chosen_consular, matched_ofc_city, matched_consular_city):
    ofc_cities_str = ", ".join(customer.get("ofc_cities", [])) or "N/A"
    consular_cities_str = ", ".join(customer.get("consular_cities", []))
    ofc_end = customer.get("ofc_end")
    ofc_end_str = ofc_end.strftime("%Y-%m-%d") if ofc_end else "N/A"

    lines = [
        f"*Customer:* {customer['customer_name']}",
        f"*Requested OFC Cities:* {ofc_cities_str}",
        f"*Requested Consular Cities:* {consular_cities_str}",
        f"*OFC Deadline:* {ofc_end_str}",
    ]

    if matched_ofc_city and chosen_ofc:
        lines += [
            f"",
            f"*Matched OFC City:* {matched_ofc_city}",
            f"*OFC Date:* {chosen_ofc['display_date']}",
            f"*OFC Slots Available:* {chosen_ofc['count']}",
        ]

    if matched_consular_city and chosen_consular:
        lines += [
            f"",
            f"*Matched Consular City:* {matched_consular_city}",
            f"*Consular Date:* {chosen_consular['display_date']}",
            f"*Consular Slots Available:* {chosen_consular['count']}",
        ]

    return "\n".join(lines)


def send_slack(msg: str):
    return send(f"🎯 *Qualified slot match found*\n{msg}", emoji="")


def send_slack_error(msg: str):
    return send(f"⚠️ *Slot Monitor Error*\n{msg}", emoji="")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Message passed as CLI argument
        msg = " ".join(sys.argv[1:])
    else:
        # Interactive prompt
        msg = input("Message: ").strip()
        if not msg:
            print("No message provided.")
            sys.exit(1)

    ok = send(msg)
    sys.exit(0 if ok else 1)
