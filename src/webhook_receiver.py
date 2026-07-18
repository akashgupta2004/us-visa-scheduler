import sys
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# Force UTF-8 output so emojis don't crash on Windows when piped
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Ensure project root is on the path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.common.state import update_state

class WebhookReceiver(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            username = data.get("username")
            updates = data.get("updates")
            
            if username and updates:
                state_file = Path(__file__).resolve().parent / f"state_{username}.json"
                update_state(state_file, updates)
                print(f"[WEBHOOK] \u2705 Received remote trigger for '{username}'!", flush=True)
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode())
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing username or updates")
                
        except Exception as e:
            print(f"[WEBHOOK] \u274c Error processing trigger: {e}", flush=True)
            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default HTTP logging to keep console clean
        pass

def run(port=8000):
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, WebhookReceiver)
    print(f"============================================================")
    print(f"\ud83c\udfa7 Webhook Receiver listening on port {port}")
    print(f"   Set REMOTE_TRIGGER_URL=http://<THIS_LAPTOP_IP>:{port}")
    print(f"   on your Polling laptop to send triggers here natively.")
    print(f"============================================================")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

if __name__ == '__main__':
    run()
