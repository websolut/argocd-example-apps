#!/usr/bin/env python3
"""
lab-app: a tiny, dependency-free HTTP server for learning Kubernetes on AKS.

Everything it does is designed to be observable from kubectl:
  * emits structured JSON logs to stdout at a configurable rate
  * exposes /healthz + /readyz that you can flip at runtime
  * can burn CPU or allocate memory on demand so the HPA has something to react to
  * exposes Prometheus metrics on /metrics
  * handles SIGTERM slowly and loudly so graceful shutdown is visible

Runs on the stock python:3-alpine image - no build, no registry, no pip install.
The whole file is mounted from a ConfigMap.
"""

import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------- configuration
PORT = int(os.getenv("PORT", "8080"))
APP_NAME = os.getenv("APP_NAME", "lab-app")
APP_VERSION = os.getenv("APP_VERSION", "dev")
APP_COLOR = os.getenv("APP_COLOR", "blue")          # handy for rollout demos
ENVIRONMENT = os.getenv("ENVIRONMENT", "local")
LOG_FORMAT = os.getenv("LOG_FORMAT", "json")        # json | text
LOG_RATE = float(os.getenv("LOG_RATE", "1"))        # background log lines / second
LOG_FILE = os.getenv("LOG_FILE", "")                # also append here (sidecar demo)
STARTUP_DELAY = float(os.getenv("STARTUP_DELAY_SECONDS", "0"))
SHUTDOWN_DELAY = float(os.getenv("SHUTDOWN_DELAY_SECONDS", "5"))

POD_NAME = os.getenv("POD_NAME", socket.gethostname())
POD_IP = os.getenv("POD_IP", "")
NODE_NAME = os.getenv("NODE_NAME", "")
NAMESPACE = os.getenv("POD_NAMESPACE", "")

STARTED_AT = time.time()


# ------------------------------------------------------------------------ state
class State:
    ready = False
    healthy = True
    log_rate = LOG_RATE
    log_lines = 0
    requests = {}          # (path, code) -> count
    burn_workers = 0
    burn_seconds_total = 0.0
    ballast = []           # holds allocated memory
    shutting_down = False


state = State()
lock = threading.Lock()
_log_lock = threading.Lock()


def log(level, message, **fields):
    """One log line to stdout (and optionally a file) - stdout is what kubectl shows."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "level": level.upper(),
        "logger": APP_NAME,
        "msg": message,
        "pod": POD_NAME,
        "node": NODE_NAME,
        "version": APP_VERSION,
        "env": ENVIRONMENT,
    }
    record.update(fields)

    if LOG_FORMAT == "text":
        extra = " ".join("{0}={1}".format(k, v) for k, v in fields.items())
        line = "{0} {1:<5} [{2}] {3} {4}".format(
            record["ts"], record["level"], POD_NAME, message, extra)
    else:
        line = json.dumps(record, default=str)

    with _log_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if LOG_FILE:
            try:
                with open(LOG_FILE, "a") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
    with lock:
        state.log_lines += 1


def count_request(path, code):
    with lock:
        key = (path, code)
        state.requests[key] = state.requests.get(key, 0) + 1


# --------------------------------------------------------------- worker threads
def chatterbox():
    """Emit steady background traffic in the logs so there is always something to tail."""
    messages = [
        ("INFO", "heartbeat", {"queue_depth": 0}),
        ("INFO", "processed batch", {"records": 42, "duration_ms": 17}),
        ("DEBUG", "cache lookup", {"hit": True, "key": "customer:1024"}),
        ("INFO", "outbound call ok", {"upstream": "payments-api", "status": 200}),
        ("WARN", "retrying upstream call", {"upstream": "payments-api", "attempt": 2}),
        ("INFO", "gc stats", {"heap_mb": 24}),
        ("ERROR", "failed to write audit record", {"reason": "timeout", "retryable": True}),
    ]
    i = 0
    while not state.shutting_down:
        rate = state.log_rate
        if rate <= 0:
            time.sleep(0.5)
            continue
        level, msg, fields = messages[i % len(messages)]
        # keep WARN/ERROR relatively rare so the stream looks realistic
        if level in ("ERROR", "WARN") and i % 3 != 0:
            level, msg, fields = messages[0]
        log(level, msg, **fields)
        i += 1
        time.sleep(1.0 / rate)


def burn_cpu(seconds):
    """Spin the CPU. This is what makes the HPA move."""
    with lock:
        state.burn_workers += 1
    log("INFO", "cpu burn started", seconds=seconds)
    deadline = time.time() + seconds
    x = 0.0001
    while time.time() < deadline and not state.shutting_down:
        for _ in range(200000):
            x = (x * 1.0000001) % 987654.321
    with lock:
        state.burn_workers -= 1
        state.burn_seconds_total += seconds
    log("INFO", "cpu burn finished", seconds=seconds)


def allocate(mb, hold_seconds):
    """Hold memory for a while - use it to trip a memory HPA target, or an OOMKill."""
    log("INFO", "allocating memory", mb=mb, hold_seconds=hold_seconds)
    block = bytearray(mb * 1024 * 1024)
    for offset in range(0, len(block), 4096):   # touch pages so they are really resident
        block[offset] = 1
    with lock:
        state.ballast.append(block)
    time.sleep(hold_seconds)
    with lock:
        try:
            state.ballast.remove(block)
        except ValueError:
            pass
    del block
    log("INFO", "released memory", mb=mb)


def become_ready():
    if STARTUP_DELAY > 0:
        log("INFO", "warming up", startup_delay_seconds=STARTUP_DELAY)
        time.sleep(STARTUP_DELAY)
    state.ready = True
    log("INFO", "ready to serve traffic")


# ------------------------------------------------------------------------ views
def render_index():
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "color": APP_COLOR,
        "environment": ENVIRONMENT,
        "pod": POD_NAME,
        "node": NODE_NAME,
        "namespace": NAMESPACE,
        "pod_ip": POD_IP,
        "ready": state.ready,
        "healthy": state.healthy,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "log_rate_per_second": state.log_rate,
        "hint": "try /info /burn /mem /slow /error /crash /toggle /lograte /metrics",
    }


def render_metrics():
    now = time.time()
    with lock:
        requests = dict(state.requests)
        allocated = sum(len(b) for b in state.ballast)
        lines_total = state.log_lines
        burn_total = state.burn_seconds_total
        burn_active = state.burn_workers
    out = [
        "# HELP lab_app_build_info Static build information.",
        "# TYPE lab_app_build_info gauge",
        'lab_app_build_info{{version="{0}",color="{1}",env="{2}"}} 1'.format(
            APP_VERSION, APP_COLOR, ENVIRONMENT),
        "# HELP lab_app_uptime_seconds Seconds since process start.",
        "# TYPE lab_app_uptime_seconds gauge",
        "lab_app_uptime_seconds {0:.1f}".format(now - STARTED_AT),
        "# HELP lab_app_ready Whether the pod reports itself ready.",
        "# TYPE lab_app_ready gauge",
        "lab_app_ready {0}".format(1 if state.ready else 0),
        "# HELP lab_app_healthy Whether the pod reports itself healthy.",
        "# TYPE lab_app_healthy gauge",
        "lab_app_healthy {0}".format(1 if state.healthy else 0),
        "# HELP lab_app_log_lines_total Log lines emitted since start.",
        "# TYPE lab_app_log_lines_total counter",
        "lab_app_log_lines_total {0}".format(lines_total),
        "# HELP lab_app_cpu_burn_workers Active CPU burn workers.",
        "# TYPE lab_app_cpu_burn_workers gauge",
        "lab_app_cpu_burn_workers {0}".format(burn_active),
        "# HELP lab_app_cpu_burn_seconds_total Requested CPU burn seconds.",
        "# TYPE lab_app_cpu_burn_seconds_total counter",
        "lab_app_cpu_burn_seconds_total {0:.1f}".format(burn_total),
        "# HELP lab_app_allocated_bytes Memory held by /mem requests.",
        "# TYPE lab_app_allocated_bytes gauge",
        "lab_app_allocated_bytes {0}".format(allocated),
        "# HELP lab_app_requests_total HTTP requests handled.",
        "# TYPE lab_app_requests_total counter",
    ]
    for (path, code), count in sorted(requests.items()):
        out.append('lab_app_requests_total{{path="{0}",code="{1}"}} {2}'.format(path, code, count))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "{0}/{1}".format(APP_NAME, APP_VERSION)

    # BaseHTTPRequestHandler logs to stderr in its own format; we do our own logging.
    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            payload = (json.dumps(body, indent=2, default=str) + "\n").encode()
        else:
            payload = str(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Pod-Name", POD_NAME)
        self.send_header("X-App-Version", APP_VERSION)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        def arg(name, default, cast=float):
            try:
                return cast(query.get(name, [default])[0])
            except (TypeError, ValueError):
                return cast(default)

        started = time.time()
        code = 200
        quiet = path in ("/healthz", "/readyz", "/metrics")

        if path in ("/", "/info"):
            self._send(200, render_index())

        elif path == "/healthz":
            code = 200 if state.healthy else 500
            self._send(code, {"status": "ok" if state.healthy else "unhealthy"})

        elif path == "/readyz":
            ready = state.ready and not state.shutting_down
            code = 200 if ready else 503
            self._send(code, {"status": "ready" if ready else "not-ready"})

        elif path == "/metrics":
            self._send(200, render_metrics(), "text/plain; version=0.0.4")

        elif path == "/burn":
            seconds = max(0.0, min(arg("seconds", 30), 600))
            workers = int(max(1, min(arg("workers", 1), 8)))
            for _ in range(workers):
                threading.Thread(target=burn_cpu, args=(seconds,), daemon=True).start()
            code = 202
            self._send(code, {"burning_seconds": seconds, "workers": workers,
                             "note": "watch it with: kubectl get hpa -w"})

        elif path == "/mem":
            mb = int(max(1, min(arg("mb", 64), 4096)))
            hold = max(1.0, min(arg("seconds", 120), 3600))
            threading.Thread(target=allocate, args=(mb, hold), daemon=True).start()
            code = 202
            self._send(code, {"allocating_mb": mb, "hold_seconds": hold})

        elif path == "/slow":
            ms = max(0.0, min(arg("ms", 2000), 60000))
            time.sleep(ms / 1000.0)
            self._send(200, {"slept_ms": ms, "pod": POD_NAME})

        elif path == "/error":
            code = int(max(400, min(arg("code", 500), 599)))
            log("ERROR", "synthetic error requested", status=code, path=path)
            self._send(code, {"error": "synthetic failure", "status": code})

        elif path == "/crash":
            log("ERROR", "crashing on request - expect a restart, then CrashLoopBackOff")
            self._send(200, {"crashing": True})
            threading.Thread(
                target=lambda: (time.sleep(0.2), os._exit(1)), daemon=True).start()

        elif path == "/toggle":
            what = query.get("what", ["ready"])[0]
            raw = query.get("value", ["false"])[0].lower()
            value = raw in ("1", "true", "yes", "on")
            if what == "health":
                state.healthy = value
            else:
                state.ready = value
            log("WARN", "state toggled by request", what=what, value=value)
            self._send(200, {"ready": state.ready, "healthy": state.healthy})

        elif path == "/lograte":
            state.log_rate = max(0.0, min(arg("rps", 1), 500))
            log("INFO", "log rate changed", log_rate_per_second=state.log_rate)
            self._send(200, {"log_rate_per_second": state.log_rate})

        elif path == "/env":
            hidden = ("SECRET", "TOKEN", "PASSWORD", "KEY", "PWD")
            safe = {k: v for k, v in sorted(os.environ.items())
                    if not any(h in k.upper() for h in hidden)}
            self._send(200, safe)

        else:
            code = 404
            self._send(404, {"error": "not found", "path": path})

        count_request(path, code)
        if not quiet:
            log("INFO", "request handled", method="GET", path=path, status=code,
                duration_ms=round((time.time() - started) * 1000, 1),
                user_agent=self.headers.get("User-Agent", "-"))

    do_POST = do_GET


# --------------------------------------------------------------------- lifecycle
def main():
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True

    def on_sigterm(signum, frame):
        # The interesting part: Kubernetes sends SIGTERM and removes the pod from the
        # Service Endpoints in parallel, then waits terminationGracePeriodSeconds
        # before SIGKILL. Keep serving during the drain window.
        log("WARN", "SIGTERM received - draining", shutdown_delay_seconds=SHUTDOWN_DELAY)
        state.ready = False
        time.sleep(SHUTDOWN_DELAY)
        state.shutting_down = True
        log("INFO", "shutdown complete", uptime_seconds=round(time.time() - STARTED_AT, 1))
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGINT, on_sigterm)

    log("INFO", "starting up", port=PORT, log_rate_per_second=state.log_rate,
        log_format=LOG_FORMAT, startup_delay_seconds=STARTUP_DELAY,
        shutdown_delay_seconds=SHUTDOWN_DELAY, log_file=LOG_FILE or None)

    threading.Thread(target=chatterbox, daemon=True).start()
    threading.Thread(target=become_ready, daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
