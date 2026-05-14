"""
routing/webhook_server.py — Lightweight webhook server for disposition writebacks.

Zapier POSTs here when a VA sets a disposition in Mojo. We find the matching
lead by phone number and update raw_leads.disposition. If the disposition
signals a bad number, we clear it and re-queue the lead for enrichment.

Endpoint:
    POST /disposition
    Headers: X-Api-Key: <WEBHOOK_API_KEY>
    Body:    {"phone": "2165551234", "disposition": "Wrong Number"}
    Returns: {"status": "ok", "updated": 1}  or  {"status": "not_found"}

GET /health — returns {"status": "ok"} for uptime checks.

CLI:
    python routing/webhook_server.py          # starts on port 5001
    python routing/webhook_server.py --port 8080

Zapier URL: http://<your-public-ip-or-ngrok>:5001/disposition
"""

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

load_dotenv()

from db.client import get_client, update_row
from utils.logger import get_logger

log = get_logger("routing.webhook_server")

WEBHOOK_API_KEY = os.getenv("WEBHOOK_API_KEY", "")
PORT = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT", 5001))

# Dispositions that mean the phone number is dead — clear and re-enrich
BAD_DISPOSITIONS = {
    "Wrong Number",
    "Disconnected",
    "Not In Service",
    "Not In Service/Disconnected",
    "Bad Number",
}


def _normalize_phone(raw: str) -> tuple[str, str]:
    """Return (e164, digits_only) for a raw phone string."""
    digits = re.sub(r"\D", "", raw)[-10:]
    return f"+1{digits}", digits


def _find_and_update(phone: str, disposition: str) -> dict:
    """Look up lead by phone, write disposition, return result dict."""
    e164, digits = _normalize_phone(phone)
    client = get_client()

    lead = None
    matched_field = None
    for field in ["phone_1", "phone_2", "phone_3"]:
        rows = (
            client.table("raw_leads")
            .select("id,phone_1,phone_2,phone_3")
            .or_(f"{field}.eq.{e164},{field}.eq.{digits}")
            .limit(1)
            .execute()
            .data or []
        )
        if rows:
            lead = rows[0]
            matched_field = field
            break

    if not lead:
        log.warning(f"Disposition webhook: no lead found for phone {phone}")
        return {"status": "not_found"}

    lead_id = lead["id"]
    updates: dict = {"disposition": disposition}

    if disposition in BAD_DISPOSITIONS:
        updates[matched_field] = None   # clear the dead number
        updates["enriched"] = False     # re-queue for Skip Sherpa
        updates["mojo_synced"] = False  # re-push to Mojo after re-enrichment
        log.info(f"Lead {lead_id[:8]}: '{disposition}' — {matched_field} cleared, re-queued")
    else:
        log.info(f"Lead {lead_id[:8]}: disposition = '{disposition}'")

    update_row("raw_leads", lead_id, updates)
    return {"status": "ok", "updated": 1, "lead_id": lead_id}


class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence default access log — our logger handles it

    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if not WEBHOOK_API_KEY:
            return True
        return self.headers.get("X-Api-Key", "") == WEBHOOK_API_KEY

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"status": "not_found"})

    def do_POST(self):
        if self.path != "/disposition":
            self._send(404, {"status": "not_found"})
            return

        if not self._authorized():
            self._send(401, {"status": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            self._send(400, {"status": "error", "message": "invalid JSON"})
            return

        phone = (data.get("phone") or "").strip()
        disposition = str(data.get("disposition") or "").strip()

        if not phone:
            self._send(400, {"status": "error", "message": "phone is required"})
            return

        try:
            result = _find_and_update(phone, disposition)
            code = 200 if result["status"] in ("ok", "not_found") else 400
            self._send(code, result)
        except Exception as e:
            log.error(f"Webhook error: {e}")
            self._send(500, {"status": "error", "message": str(e)})


def start(port: int = PORT, block: bool = True) -> HTTPServer | None:
    """Start the webhook server. If block=False, runs in a daemon thread."""
    server = HTTPServer(("0.0.0.0", port), _Handler)
    log.info(f"Webhook server listening on port {port}")
    log.info(f"  Health:      GET  http://localhost:{port}/health")
    log.info(f"  Disposition: POST http://localhost:{port}/disposition")

    if block:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Lead Intel webhook server")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    start(port=args.port, block=True)
