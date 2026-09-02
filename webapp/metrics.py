"""Read-only system metrics for the console dashboard (CPU / RAM / Storage).

All values come from safe, unprivileged, stdlib-only sources - the Flask
application reads them directly and never needs sudo, ``web-ctl``, or any
shell command:

* CPU     - aggregate counters from ``/proc/stat`` sampled twice with a short
            bounded pause (no background sampler, no threads).
* CPU     - logical core count from ``os.cpu_count()`` (count only, never
            model/vendor/flags).
* RAM     - ``/proc/meminfo`` ``MemTotal`` / ``MemAvailable`` (used is defined
            as ``MemTotal - MemAvailable`` so cache/buffers stay available;
            KiB values from meminfo are converted to bytes with ``* 1024``).
* Storage - ``shutil.disk_usage()`` on the filesystem that hosts BlueStream's
            persistent media/state (see app.py's WEB_UPLOAD_DIR).

Every metric fails independently to ``None`` so a broken source can never take
down the dashboard. Percentages are normalized to the safe 0-100 range and no
browser-supplied value ever influences a measurement.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

# Short bounded pause between the two /proc/stat samples that produce a CPU
# utilization reading. There is intentionally no background sampling.
CPU_SAMPLE_INTERVAL_SECONDS = 0.2

# Exact JSON keys returned by collect_metrics() / the console endpoint.
METRIC_KEYS = (
    "cpu_percent",
    "cpu_count",
    "ram_percent",
    "ram_used_bytes",
    "ram_total_bytes",
    "storage_percent",
    "storage_used_bytes",
    "storage_total_bytes",
)


def _clamp_percent(value) -> float:
    """Normalize a percentage to the safe 0-100 range (0 when unusable)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number < 0.0:
        return 0.0
    if number > 100.0:
        return 100.0
    return number


# ---------------------------------------------------------------------------
# CPU (Linux /proc/stat aggregate counters)
# ---------------------------------------------------------------------------
def cpu_counters(line: str) -> tuple | None:
    """Parse the aggregate ``cpu`` line of /proc/stat into integer counters.

    The aggregate line looks like::

        cpu  100 50 80 1000 100 0 0 0 0 0

    (user nice system idle iowait irq softirq steal guest guest_nice).

    Returns the first eight counters (user..steal) because ``guest`` and
    ``guest_nice`` are already counted inside ``user``/``nice`` by the kernel
    and must not be double-counted in the total. Returns None for any
    malformed/foreign line so callers fail safely.
    """
    if not isinstance(line, str):
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu" or len(parts) < 5:
        return None
    values = []
    for token in parts[1:]:
        try:
            values.append(int(token))
        except (TypeError, ValueError):
            return None
    return tuple(values[:8])


def _cpu_deltas(sample_a, sample_b):
    """Return (busy_delta, total_delta) between two /proc/stat CPU samples.

    ``idle`` is treated as ``idle + iowait`` (matching the kernel's own
    accounting). Both deltas are clamped to zero; None when either sample is
    unusable. The caller must guard ``total_delta == 0`` before dividing.
    """
    if sample_a is None or sample_b is None:
        return None
    if len(sample_a) < 4 or len(sample_b) < 4:
        return None

    def idle(values):
        iowait = values[4] if len(values) > 4 else 0
        return values[3] + iowait

    total_a, total_b = sum(sample_a), sum(sample_b)
    idle_a, idle_b = idle(sample_a), idle(sample_b)
    total_delta = max(total_b - total_a, 0)
    busy_delta = max(total_delta - (idle_b - idle_a), 0)
    return busy_delta, total_delta


def cpu_percent(sample_a, sample_b) -> float | None:
    """CPU utilization percent (0-100) from two /proc/stat samples.

    * Returns None when the samples are unusable (missing/foreign data).
    * Returns 0.0 when there is no measurable delta between samples, so the
      calculation can never divide by zero.
    * The result is clamped to the safe 0-100 range.
    """
    deltas = _cpu_deltas(sample_a, sample_b)
    if deltas is None:
        return None
    busy, total = deltas
    if total <= 0:
        return 0.0
    return _clamp_percent((busy / total) * 100.0)


def _read_stat_cpu(stat_path) -> tuple | None:
    """Read the aggregate ``cpu`` counters from /proc/stat (or None)."""
    try:
        with open(stat_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0] == "cpu":
                    return cpu_counters(line)
    except OSError:
        return None
    return None


def _collect_cpu(stat_path, sample_interval) -> float | None:
    """Two short bounded /proc/stat samples -> CPU usage percent or None.

    No background sampler: both reads happen inside the single request.
    """
    first = _read_stat_cpu(stat_path)
    if first is None:
        return None
    time.sleep(sample_interval)
    second = _read_stat_cpu(stat_path)
    if second is None:
        return None
    return cpu_percent(first, second)


def _collect_cpu_count() -> int | None:
    """Logical CPU core count via the safe stdlib ``os.cpu_count()``.

    Returns an integer >= 1 when available; None for any unusable result
    (``None``, ``0``, non-integer, or a read error). No subprocess/shell.
    """
    try:
        count = os.cpu_count()
    except (OSError, ValueError):
        return None
    if isinstance(count, int) and count >= 1:
        return count
    return None


# ---------------------------------------------------------------------------
# RAM (Linux /proc/meminfo)
# ---------------------------------------------------------------------------
def _parse_meminfo(text: str) -> dict | None:
    """Parse /proc/meminfo ``Key: N kB`` lines into a dict of integers."""
    if not isinstance(text, str):
        return None
    values = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        token = rest.strip().split()
        if not token:
            continue
        try:
            values[key.strip()] = int(token[0])
        except (TypeError, ValueError):
            continue
    return values


def ram_metrics(meminfo_text: str) -> dict | None:
    """Return {ram_percent, ram_used_bytes, ram_total_bytes} from meminfo.

    ``/proc/meminfo`` reports memory in KiB, so every parsed value is
    converted to real bytes with ``bytes = meminfo_kib * 1024`` before it is
    exposed. ``used = MemTotal - MemAvailable`` so cache/buffers are NOT
    counted as permanently used RAM. Returns None for missing or malformed
    data (e.g. a kernel without MemAvailable), never raises.
    """
    values = _parse_meminfo(meminfo_text)
    if not values:
        return None
    total_kib = values.get("MemTotal")
    available_kib = values.get("MemAvailable")
    if not isinstance(total_kib, int) or total_kib <= 0:
        return None
    if not isinstance(available_kib, int) or available_kib < 0:
        return None
    total_bytes = total_kib * 1024
    available_bytes = available_kib * 1024
    used_bytes = max(total_bytes - available_bytes, 0)
    return {
        "ram_percent": _clamp_percent((used_bytes / total_bytes) * 100.0),
        "ram_used_bytes": used_bytes,
        "ram_total_bytes": total_bytes,
    }


def _collect_ram(meminfo_path) -> dict | None:
    """Read /proc/meminfo and compute RAM metrics (or None on failure)."""
    try:
        text = Path(meminfo_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return ram_metrics(text)


# ---------------------------------------------------------------------------
# Storage (shutil.disk_usage on the BlueStream data filesystem)
# ---------------------------------------------------------------------------
def storage_metrics(path, disk_usage=None) -> dict | None:
    """Return {storage_percent, storage_used_bytes, storage_total_bytes}.

    Uses ``shutil.disk_usage`` (or an injected callable in tests). Returns
    None on any read failure or implausible value; percentages and byte
    counts are normalized to safe bounds.
    """
    if disk_usage is None:
        disk_usage = shutil.disk_usage
    try:
        usage = disk_usage(path)
    except (OSError, ValueError, TypeError):
        return None
    try:
        total = int(getattr(usage, "total", 0))
        used = int(getattr(usage, "used", 0))
    except (TypeError, ValueError):
        return None
    if total <= 0 or used < 0:
        return None
    used = min(used, total)
    return {
        "storage_percent": _clamp_percent((used / total) * 100.0),
        "storage_used_bytes": used,
        "storage_total_bytes": total,
    }


def _collect_storage(storage_path) -> dict | None:
    """Collect storage metrics for the given filesystem path (or None)."""
    if not storage_path:
        return None
    return storage_metrics(storage_path)


# ---------------------------------------------------------------------------
# Combined collection (each metric fails independently)
# ---------------------------------------------------------------------------
def collect_metrics(
    stat_path="/proc/stat",
    meminfo_path="/proc/meminfo",
    storage_path=None,
    sample_interval=CPU_SAMPLE_INTERVAL_SECONDS,
) -> dict:
    """Collect every dashboard metric; each one fails independently to None.

    The returned dict uses exactly :data:`METRIC_KEYS` and never contains
    filesystem paths, process lists, usernames, or any shell/configuration
    output.
    """
    metrics = dict.fromkeys(METRIC_KEYS)
    cpu = _collect_cpu(stat_path, sample_interval)
    if cpu is not None:
        metrics["cpu_percent"] = round(_clamp_percent(cpu), 1)
    metrics["cpu_count"] = _collect_cpu_count()
    ram = _collect_ram(meminfo_path)
    if ram is not None:
        metrics["ram_percent"] = round(_clamp_percent(ram.get("ram_percent")), 1)
        metrics["ram_used_bytes"] = ram.get("ram_used_bytes")
        metrics["ram_total_bytes"] = ram.get("ram_total_bytes")
    storage = _collect_storage(storage_path)
    if storage is not None:
        metrics["storage_percent"] = round(
            _clamp_percent(storage.get("storage_percent")), 1
        )
        metrics["storage_used_bytes"] = storage.get("storage_used_bytes")
        metrics["storage_total_bytes"] = storage.get("storage_total_bytes")
    return metrics
