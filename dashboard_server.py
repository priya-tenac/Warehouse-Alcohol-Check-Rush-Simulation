#!/usr/bin/env python3
import contextlib
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from warehouse_sim import Simulation, build_parser

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "dashboard.html"

FIELD_TYPES = {
    "workers": int,
    "buses": int,
    "bus_delay": float,
    "bus_gap": float,
    "test_time": float,
    "retest_wait": float,
    "invalid_rate": float,
    "wrong_rate": float,
    "alcohol_rate": float,
    "contamination": float,
    "patience": float,
    "sneak_rate": float,
    "backup_machines": int,
    "max_attempts": int,
    "breakdown_rate": float,
    "repair_time": float,
    "return_rate": float,
    "max_minutes": float,
    "speed": float,
    "seed": int,
}


def coerce_value(name, value):
    if value is None or value == "":
        return None
    target_type = FIELD_TYPES.get(name, str)
    if target_type is bool:
        return str(value).lower() in {"1", "true", "yes", "on"}
    if target_type is int:
        return int(float(value))
    if target_type is float:
        return float(value)
    return str(value)


def build_cfg(payload):
    parser = build_parser()
    cfg = parser.parse_args([])
    for key, value in payload.items():
        if not hasattr(cfg, key):
            continue
        coerced = coerce_value(key, value)
        if coerced is not None:
            setattr(cfg, key, coerced)
    return cfg


def run_simulation(payload):
    cfg = build_cfg(payload)
    sim = Simulation(cfg)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        sim.run()

    inside = [e for e in sim.employees if e.status == "INSIDE"]
    home = [e for e in sim.employees if e.status == "SENT_HOME"]
    manual = [e for e in sim.employees if e.status == "MANUAL_REVIEW"]
    avg_wait = sum(e.wait_total for e in sim.employees) / len(sim.employees) if sim.employees else 0.0

    summary = {
        "entered": len(inside),
        "sent_home": len(home),
        "manual_review": len(manual),
        "avg_wait": round(avg_wait, 1),
        "longest_wait": round(sim.max_wait, 1),
        "security_incidents": len(sim.incidents),
        "machines_used": sim.machines_started,
        "machine_breakdowns": sim.stats.get("breakdowns", 0),
        "tests_run": sim.stats.get("tests", 0),
        "retests": sim.stats.get("retests", 0),
        "invalid_readings": sim.stats.get("invalid", 0),
        "sober_wrongly_sent_home": sim.stats.get("wrong_reject", 0),
        "total_time": round(sim.clock.now(), 1),
        "report": stdout.getvalue().strip(),
        "incidents": [
            {"time": t, "employee": n, "status": st}
            for t, n, st in sim.incidents
        ],
    }
    return summary


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "WarehouseDashboard/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self.serve_file(INDEX_FILE)
            return
        if parsed.path == "/api/run":
            params = parse_qs(parsed.query)
            payload = {key: value[0] for key, value in params.items()}
            data = run_simulation(payload)
            self.send_json(data)
            return
        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            data = run_simulation(payload)
            self.send_json(data)
            return
        self.send_error(404, "Not Found")

    def serve_file(self, path):
        content = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def main():
    host = "127.0.0.1"
    port = 8000
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Warehouse dashboard running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
