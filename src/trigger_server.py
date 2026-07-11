import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sys

# Ensure project root is on the path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.common.state import update_state, get_state_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TRIGGER-SERVER] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("trigger_server")

class TriggerHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/trigger":
            self.send_response(404)
            self.end_headers()
            return
            
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            payload = json.loads(post_data)
            username = payload.get("username")
            updates = payload.get("updates")
            
            if not username or not updates:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Missing username or updates"}')
                return
                
            state_file = get_state_file(username)
            update_state(state_file, updates)
            
            log.info(f"✅ Received and wrote remote trigger for {username} -> {updates.get('action_type', 'UPDATE')}")
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "success"}')
            
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid JSON"}')
        except Exception as e:
            log.error(f"Error processing trigger: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Internal server error"}')

def run(port=8000):
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, TriggerHandler)
    log.info(f"🚀 Trigger Server listening on {server_address[0]}:{server_address[1]}")
    log.info("Ready to receive triggers from Laptop A...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log.info("Server stopped.")

if __name__ == '__main__':
    run()
