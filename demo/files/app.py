#!/usr/bin/env python3
"""
demo-app: a tiny, dependency-free HTTP server for learning AKS storage + scaling.

Its sibling chart in this repo (../lab-app) covers probes, sidecars and
rollouts. This one is about the two things that chart deliberately leaves out:

  * HPA               - burn CPU or hold memory on demand so the autoscaler
                        has something real to react to
  * PersistentVolumes - write, read, fill, fsync and measure a real volume, so
                        the difference between an emptyDir, an Azure Disk (RWO)
                        and an Azure File share (RWX) is something you observe
                        rather than something you read about

Runs on the stock python:3-alpine image - no build, no registry, no pip install.
The whole file is mounted from a ConfigMap.
"""

import errno
import json
import os
import shutil
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------- configuration
PORT = int(os.getenv("PORT", "8080"))
APP_NAME = os.getenv("APP_NAME", "demo-app")
APP_VERSION = os.getenv("APP_VERSION", "dev")
ENVIRONMENT = os.getenv("ENVIRONMENT", "local")
LOG_RATE = float(os.getenv("LOG_RATE", "1"))        # background log lines / second
STARTUP_DELAY = float(os.getenv("STARTUP_DELAY_SECONDS", "0"))
SHUTDOWN_DELAY = float(os.getenv("SHUTDOWN_DELAY_SECONDS", "5"))

# Where the volume is mounted. The chart always mounts something here - an
# emptyDir, one shared PVC, or a PVC per pod - so the app code never changes;
# only the volume behind it does. That is the whole point of the exercise.
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", "/data"))
STORAGE_MODE = os.getenv("STORAGE_MODE", "none")    # none | shared | perPod

POD_NAME = os.getenv("POD_NAME", socket.gethostname())
POD_IP = os.getenv("POD_IP", "")
NODE_NAME = os.getenv("NODE_NAME", "")
NAMESPACE = os.getenv("POD_NAMESPACE", "")

STARTED_AT = time.time()
BOOT_ID = "{0}-{1}".format(POD_NAME, int(STARTED_AT))


# ------------------------------------------------------------------------ state
class State:
    ready = False
    healthy = True
    log_rate = LOG_RATE
    log_lines = 0
    requests = {}           # (path, code) -> count
    burn_workers = 0
    burn_seconds_total = 0.0
    ballast = []            # holds allocated memory
    shutting_down = False
    # storage counters
    bytes_written = 0
    bytes_read = 0
    write_errors = 0
    last_write_ms = 0.0
    last_fsync_ms = 0.0
    storage_writable = False


state = State()
lock = threading.Lock()
_log_lock = threading.Lock()


def log(level, message, **fields):
    """One JSON log line to stdout - stdout is what kubectl shows."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "level": level.upper(),
        "logger": APP_NAME,
        "msg": message,
        "pod": POD_NAME,
        "node": NODE_NAME,
        "version": APP_VERSION,
        "env": ENVIRONMENT,
        "storage_mode": STORAGE_MODE,
    }
    record.update(fields)
    with _log_lock:
        sys.stdout.write(json.dumps(record, default=str) + "\n")
        sys.stdout.flush()
    with lock:
        state.log_lines += 1


def count_request(path, code):
    with lock:
        key = (path, code)
        state.requests[key] = state.requests.get(key, 0) + 1


# ---------------------------------------------------------------------- storage
# Everything below treats DATA_DIR as an ordinary directory - because that is
# exactly what a PersistentVolume looks like from inside a container. All the
# interesting behaviour (does it survive a restart? can two pods see it? how
# slow is fsync?) comes from which volume the chart put there, not from here.

def data_path(name):
    """
    Resolve a name inside DATA_DIR, refusing anything that escapes it.

    The endpoints take a file name from the query string, so this is the only
    thing standing between a lab exercise and `?name=../../etc/passwd`.
    abspath() collapses any "..", then the prefix check rejects the result if it
    landed outside the volume.
    """
    clean = str(name).replace("\\", "/").strip("/")
    if not clean:
        raise ValueError("invalid name")
    full = os.path.abspath(os.path.join(DATA_DIR, *clean.split("/")))
    if full != DATA_DIR and not full.startswith(DATA_DIR + os.sep):
        raise ValueError("invalid name")
    return full


def disk_usage():
    """Capacity of the mounted volume - i.e. what `kubectl get pvc` promised."""
    try:
        total, used, free = shutil.disk_usage(DATA_DIR)
    except OSError as exc:
        return {"error": str(exc)}
    return {
        "path": DATA_DIR,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "total_mb": round(total / 1048576.0, 1),
        "used_mb": round(used / 1048576.0, 1),
        "free_mb": round(free / 1048576.0, 1),
        "used_percent": round(used * 100.0 / total, 1) if total else 0.0,
    }


def filesystem():
    """
    The mount as the kernel sees it, which is worth a look: an Azure Disk shows
    up as ext4, an Azure File share as cifs, and an emptyDir as whatever the
    node root filesystem is (usually overlay). Different filesystem means
    different rules for sharing, locking and fsync latency.
    """
    best = None
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                mount_point = parts[1]
                # The longest matching mount point is the one we are actually on.
                if DATA_DIR == mount_point or DATA_DIR.startswith(mount_point.rstrip("/") + "/"):
                    if best is None or len(mount_point) > len(best[1]):
                        best = parts
    except OSError as exc:
        return {"error": str(exc)}
    if best is None:
        return {"note": "no matching mount found"}
    return {"device": best[0], "mount_point": best[1],
            "fstype": best[2], "options": best[3]}


def record_boot():
    """
    Append one line per process start to a file on the volume.

    This one file answers the questions the storage labs are about:
      * emptyDir          -> the file only ever has one line; every restart
                             starts from nothing
      * shared PVC (RWX)  -> every replica appends, so you see all of them
      * per-pod PVC (RWO) -> each pod sees only its own history, and that
                             history survives rescheduling onto another node
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(data_path("boots.log"), "a") as fh:
            fh.write(json.dumps({
                "boot_id": BOOT_ID,
                "pod": POD_NAME,
                "node": NODE_NAME,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                "version": APP_VERSION,
            }) + "\n")
        with lock:
            state.storage_writable = True
        log("INFO", "boot recorded on volume", boot_id=BOOT_ID, data_dir=DATA_DIR,
            filesystem=filesystem().get("fstype"))
    except OSError as exc:
        with lock:
            state.storage_writable = False
            state.write_errors += 1
        # An unwritable mount is a real AKS failure mode, not a hypothetical:
        # an Azure Disk with the wrong fsGroup, or a File share mounted without
        # uid/gid mount options, both land here.
        log("ERROR", "volume is not writable", data_dir=DATA_DIR, reason=str(exc),
            hint="check fsGroup on the pod and mountOptions on the StorageClass")


def read_boots():
    try:
        with open(data_path("boots.log")) as fh:
            return [json.loads(x) for x in fh if x.strip()]
    except (OSError, ValueError):
        return []


def write_blob(name, size_bytes, fsync):
    """Write one file and time it. fsync forces it out to the actual device."""
    payload = b"x" * 65536
    path = data_path(name)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    started = time.time()
    written = 0
    fsync_ms = None
    with open(path, "wb") as fh:
        while written < size_bytes:
            chunk = min(len(payload), size_bytes - written)
            fh.write(payload[:chunk])
            written += chunk
        fh.flush()
        if fsync:
            # Without this you are timing the page cache, not the volume. On an
            # Azure File share the difference is dramatic; on a Premium SSD it
            # is small but real.
            fsync_started = time.time()
            os.fsync(fh.fileno())
            fsync_ms = round((time.time() - fsync_started) * 1000, 2)
    elapsed = time.time() - started

    with lock:
        state.bytes_written += written
        state.last_write_ms = round(elapsed * 1000, 2)
        if fsync_ms is not None:
            state.last_fsync_ms = fsync_ms
    return {
        "file": name,
        "bytes": written,
        "duration_ms": round(elapsed * 1000, 2),
        "throughput_mb_s": round(written / 1048576.0 / elapsed, 1) if elapsed > 0 else None,
        "fsync": bool(fsync),
        "fsync_ms": fsync_ms,
    }


def fill_volume(target_percent, chunk_mb):
    """
    Keep writing until the volume is target_percent full, or until ENOSPC.

    Filling a PVC is the point of the exercise: a full volume does not
    autoscale. You resize it - which needs allowVolumeExpansion on the
    StorageClass - and that is a completely different operation from adding
    replicas. Conflating the two is the usual reason a "scaled" app stays down.
    """
    log("WARN", "filling volume", target_percent=target_percent, chunk_mb=chunk_mb)
    index = 0
    while not state.shutting_down:
        usage = disk_usage()
        if "error" in usage or usage["used_percent"] >= target_percent:
            break
        try:
            write_blob("fill/blob-{0:04d}".format(index), int(chunk_mb * 1048576), False)
        except OSError as exc:
            with lock:
                state.write_errors += 1
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                log("ERROR", "volume full", used_percent=disk_usage().get("used_percent"),
                    hint="expand it: kubectl patch pvc <name> -p "
                         "'{\"spec\":{\"resources\":{\"requests\":{\"storage\":\"8Gi\"}}}}'")
            else:
                log("ERROR", "write failed", reason=str(exc))
            break
        index += 1
    log("INFO", "fill finished", usage=disk_usage())


# --------------------------------------------------------------- worker threads
def chatterbox():
    """Steady background log traffic, so there is always something to tail."""
    messages = [
        ("INFO", "heartbeat", {"queue_depth": 0}),
        ("INFO", "processed batch", {"records": 42, "duration_ms": 17}),
        ("INFO", "checkpoint written", {"target": DATA_DIR}),
        ("INFO", "outbound call ok", {"upstream": "payments-api", "status": 200}),
        ("WARN", "retrying upstream call", {"upstream": "payments-api", "attempt": 2}),
    ]
    i = 0
    while not state.shutting_down:
        rate = state.log_rate
        if rate <= 0:
            time.sleep(0.5)
            continue
        level, msg, fields = messages[i % len(messages)]
        # keep WARN relatively rare so the stream looks realistic
        if level == "WARN" and i % 3 != 0:
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
    """Hold memory - trips a memory HPA target, or an OOMKill if you overdo it."""
    log("INFO", "allocating memory", mb=mb, hold_seconds=hold_seconds)
    block = bytearray(mb * 1024 * 1024)
    for offset in range(0, len(block), 4096):   # touch pages so they are resident
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
    record_boot()
    if STARTUP_DELAY > 0:
        log("INFO", "warming up", startup_delay_seconds=STARTUP_DELAY)
        time.sleep(STARTUP_DELAY)
    state.ready = True
    log("INFO", "ready to serve traffic")


# ------------------------------------------------------------------------ views
def render_index():
    boots = read_boots()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "environment": ENVIRONMENT,
        "pod": POD_NAME,
        "node": NODE_NAME,
        "namespace": NAMESPACE,
        "pod_ip": POD_IP,
        "ready": state.ready,
        "healthy": state.healthy,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "storage": {
            "mode": STORAGE_MODE,
            "data_dir": DATA_DIR,
            "writable": state.storage_writable,
            "filesystem": filesystem(),
            "usage": disk_usage(),
            # How many process starts this volume remembers. 1 means nothing is
            # surviving; more than 1 means the data outlived a pod.
            "boots_recorded": len(boots),
            "distinct_pods_seen": len(set(b.get("pod") for b in boots)),
        },
        "hint": "try /info /burn /mem /df /write /read /ls /fill /boots /mounts /metrics",
    }


def render_metrics():
    now = time.time()
    usage = disk_usage()
    with lock:
        requests = dict(state.requests)
        allocated = sum(len(b) for b in state.ballast)
        lines_total = state.log_lines
        burn_total = state.burn_seconds_total
        burn_active = state.burn_workers
        written = state.bytes_written
        read_total = state.bytes_read
        write_errors = state.write_errors
        write_ms = state.last_write_ms
        fsync_ms = state.last_fsync_ms
    out = [
        "# HELP demo_app_build_info Static build information.",
        "# TYPE demo_app_build_info gauge",
        'demo_app_build_info{{version="{0}",env="{1}",storage_mode="{2}"}} 1'.format(
            APP_VERSION, ENVIRONMENT, STORAGE_MODE),
        "# HELP demo_app_uptime_seconds Seconds since process start.",
        "# TYPE demo_app_uptime_seconds gauge",
        "demo_app_uptime_seconds {0:.1f}".format(now - STARTED_AT),
        "# HELP demo_app_ready Whether the pod reports itself ready.",
        "# TYPE demo_app_ready gauge",
        "demo_app_ready {0}".format(1 if state.ready else 0),
        "# HELP demo_app_healthy Whether the pod reports itself healthy.",
        "# TYPE demo_app_healthy gauge",
        "demo_app_healthy {0}".format(1 if state.healthy else 0),
        "# HELP demo_app_log_lines_total Log lines emitted since start.",
        "# TYPE demo_app_log_lines_total counter",
        "demo_app_log_lines_total {0}".format(lines_total),
        # ---- the HPA series --------------------------------------------------
        "# HELP demo_app_cpu_burn_workers Active CPU burn workers.",
        "# TYPE demo_app_cpu_burn_workers gauge",
        "demo_app_cpu_burn_workers {0}".format(burn_active),
        "# HELP demo_app_cpu_burn_seconds_total Requested CPU burn seconds.",
        "# TYPE demo_app_cpu_burn_seconds_total counter",
        "demo_app_cpu_burn_seconds_total {0:.1f}".format(burn_total),
        "# HELP demo_app_allocated_bytes Memory held by /mem requests.",
        "# TYPE demo_app_allocated_bytes gauge",
        "demo_app_allocated_bytes {0}".format(allocated),
        # ---- the storage series: graph these while you run the labs ----------
        "# HELP demo_app_volume_writable Whether the mounted volume accepts writes.",
        "# TYPE demo_app_volume_writable gauge",
        "demo_app_volume_writable {0}".format(1 if state.storage_writable else 0),
        "# HELP demo_app_volume_bytes_total Capacity of the mounted volume.",
        "# TYPE demo_app_volume_bytes_total gauge",
        "demo_app_volume_bytes_total {0}".format(usage.get("total_bytes", 0)),
        "# HELP demo_app_volume_used_bytes Bytes used on the mounted volume.",
        "# TYPE demo_app_volume_used_bytes gauge",
        "demo_app_volume_used_bytes {0}".format(usage.get("used_bytes", 0)),
        "# HELP demo_app_volume_free_bytes Bytes free on the mounted volume.",
        "# TYPE demo_app_volume_free_bytes gauge",
        "demo_app_volume_free_bytes {0}".format(usage.get("free_bytes", 0)),
        "# HELP demo_app_written_bytes_total Bytes written by /write and /fill.",
        "# TYPE demo_app_written_bytes_total counter",
        "demo_app_written_bytes_total {0}".format(written),
        "# HELP demo_app_read_bytes_total Bytes read by /read.",
        "# TYPE demo_app_read_bytes_total counter",
        "demo_app_read_bytes_total {0}".format(read_total),
        "# HELP demo_app_write_errors_total Failed writes (ENOSPC, read-only mount).",
        "# TYPE demo_app_write_errors_total counter",
        "demo_app_write_errors_total {0}".format(write_errors),
        "# HELP demo_app_last_write_milliseconds Duration of the most recent write.",
        "# TYPE demo_app_last_write_milliseconds gauge",
        "demo_app_last_write_milliseconds {0}".format(write_ms),
        "# HELP demo_app_last_fsync_milliseconds Duration of the most recent fsync.",
        "# TYPE demo_app_last_fsync_milliseconds gauge",
        "demo_app_last_fsync_milliseconds {0}".format(fsync_ms),
        "# HELP demo_app_requests_total HTTP requests handled.",
        "# TYPE demo_app_requests_total counter",
    ]
    for (path, code), count in sorted(requests.items()):
        out.append('demo_app_requests_total{{path="{0}",code="{1}"}} {2}'.format(
            path, code, count))
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "{0}/{1}".format(APP_NAME, APP_VERSION)

    # BaseHTTPRequestHandler logs to stderr in its own format; we do our own.
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

        def flag(name, default="1"):
            return query.get(name, [default])[0].lower() in ("1", "true", "yes", "on")

        started = time.time()
        code = 200
        quiet = path in ("/healthz", "/readyz", "/metrics")

        # -------------------------------------------------------------- basics
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

        # ----------------------------------------------------------------- hpa
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

        # ------------------------------------------------------------- storage
        elif path in ("/df", "/storage"):
            self._send(200, {"mode": STORAGE_MODE, "data_dir": DATA_DIR,
                             "writable": state.storage_writable,
                             "usage": disk_usage(), "filesystem": filesystem()})

        elif path == "/mounts":
            self._send(200, filesystem())

        elif path == "/write":
            # /write?mb=64&fsync=1 - one timed write against the real volume
            mb = max(0.001, min(arg("mb", 8), 4096))
            name = query.get("name", ["data/{0}.bin".format(BOOT_ID)])[0]
            try:
                result = write_blob(name, int(mb * 1048576), flag("fsync"))
                result["pod"] = POD_NAME
                result["usage"] = disk_usage()
                self._send(200, result)
            except (OSError, ValueError) as exc:
                with lock:
                    state.write_errors += 1
                code = 507 if getattr(exc, "errno", None) == errno.ENOSPC else 400
                log("ERROR", "write failed", reason=str(exc), name=name, status=code)
                self._send(code, {"error": str(exc), "usage": disk_usage()})

        elif path == "/read":
            name = query.get("name", ["boots.log"])[0]
            try:
                full = data_path(name)
                size = os.path.getsize(full)
                with open(full, "rb") as fh:
                    head = fh.read(2048)
                with lock:
                    state.bytes_read += size
                self._send(200, {"file": name, "bytes": size, "pod": POD_NAME,
                                 "head": head.decode("utf-8", "replace")})
            except (OSError, ValueError) as exc:
                code = 404
                self._send(code, {"error": str(exc), "file": name})

        elif path == "/ls":
            # Walk the volume. Run it against each replica (the X-Pod-Name
            # header tells you which one answered) to find out whether they are
            # looking at the same bytes or at their own private copy.
            entries = []
            total = 0
            try:
                for root, _dirs, files in os.walk(DATA_DIR):
                    for fname in files:
                        full = os.path.join(root, fname)
                        try:
                            info = os.stat(full)
                        except OSError:
                            continue    # deleted underneath us; fine
                        total += info.st_size
                        entries.append({
                            "name": os.path.relpath(full, DATA_DIR),
                            "bytes": info.st_size,
                            "modified": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.st_mtime)),
                        })
            except OSError as exc:
                code = 500
                self._send(code, {"error": str(exc)})
            else:
                entries.sort(key=lambda e: e["name"])
                self._send(200, {"data_dir": DATA_DIR, "pod": POD_NAME,
                                 "files": len(entries), "bytes": total,
                                 "entries": entries[:200], "usage": disk_usage()})

        elif path == "/rm":
            name = query.get("name", [""])[0]
            try:
                full = data_path(name)
                if os.path.isdir(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
                log("WARN", "deleted from volume", name=name)
                self._send(200, {"deleted": name, "usage": disk_usage()})
            except (OSError, ValueError) as exc:
                code = 404
                self._send(code, {"error": str(exc), "name": name})

        elif path == "/fill":
            percent = max(1.0, min(arg("percent", 90), 100))
            chunk = max(1.0, min(arg("chunk_mb", 16), 1024))
            threading.Thread(target=fill_volume, args=(percent, chunk), daemon=True).start()
            code = 202
            self._send(code, {"filling_to_percent": percent, "chunk_mb": chunk,
                              "note": "clean up afterwards with /rm?name=fill"})

        elif path == "/boots":
            boots = read_boots()
            self._send(200, {
                "pod": POD_NAME,
                "storage_mode": STORAGE_MODE,
                "boots_recorded": len(boots),
                "distinct_pods_seen": sorted(set(b.get("pod", "?") for b in boots)),
                "reading": "1 boot = nothing persisted; several pods = a shared "
                           "(RWX) volume; several boots but one pod = a per-pod "
                           "(RWO) volume that survived a restart",
                "boots": boots[-50:],
            })

        # -------------------------------------------------------- misc levers
        elif path == "/slow":
            ms = max(0.0, min(arg("ms", 2000), 60000))
            time.sleep(ms / 1000.0)
            self._send(200, {"slept_ms": ms, "pod": POD_NAME})

        elif path == "/error":
            code = int(max(400, min(arg("code", 500), 599)))
            log("ERROR", "synthetic error requested", status=code, path=path)
            self._send(code, {"error": "synthetic failure", "status": code})

        elif path == "/crash":
            # Especially useful here: crash, then curl /boots on the restarted
            # pod. The boot count tells you whether the volume kept the data.
            log("ERROR", "crashing on request - expect a restart, then CrashLoopBackOff")
            self._send(200, {"crashing": True})
            threading.Thread(
                target=lambda: (time.sleep(0.2), os._exit(1)), daemon=True).start()

        elif path == "/toggle":
            what = query.get("what", ["ready"])[0]
            value = flag("value", "false")
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
        # Kubernetes sends SIGTERM and removes the pod from the Service
        # endpoints in parallel, then waits terminationGracePeriodSeconds before
        # SIGKILL. Keep serving during the drain window. For a pod with a
        # volume attached this window is also the only chance to flush anything,
        # and on a StatefulSet the disk cannot detach until the pod is gone.
        log("WARN", "SIGTERM received - draining", shutdown_delay_seconds=SHUTDOWN_DELAY)
        state.ready = False
        time.sleep(SHUTDOWN_DELAY)
        state.shutting_down = True
        log("INFO", "shutdown complete", uptime_seconds=round(time.time() - STARTED_AT, 1),
            bytes_written=state.bytes_written)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGINT, on_sigterm)

    log("INFO", "starting up", port=PORT, data_dir=DATA_DIR, storage_mode=STORAGE_MODE,
        log_rate_per_second=state.log_rate, startup_delay_seconds=STARTUP_DELAY,
        shutdown_delay_seconds=SHUTDOWN_DELAY)

    threading.Thread(target=chatterbox, daemon=True).start()
    threading.Thread(target=become_ready, daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
