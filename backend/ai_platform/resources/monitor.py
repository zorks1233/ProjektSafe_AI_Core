"""Resource monitoring & adaptive management (RAM/CPU/GPU).

Uses psutil when available for real readings; falls back to /proc parsing so
the module works on minimal Linux systems without extra installs. GPU data is
read via nvidia-smi when present. The router consumes `snapshot()` to make
capability decisions (e.g. refuse cloud-heavy routes under memory pressure).
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

try:  # optional, preferred
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None


@dataclass
class ResourceSnapshot:
    cpu_percent: float
    ram_total_mb: float
    ram_available_mb: float
    ram_percent: float
    gpu_available: bool
    gpu_name: str
    gpu_mem_used_mb: int
    gpu_mem_total_mb: int
    load_pressure: float          # 0..1 composite signal
    sampled_at: float


def _proc_meminfo() -> tuple[float, float]:
    total = avail = 0.0
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemTotal"):
                total = int(line.split()[1]) / 1024.0
            elif line.startswith("MemAvailable"):
                avail = int(line.split()[1]) / 1024.0
    return total, avail


def _proc_cpu_percent(prev: tuple[int, int]) -> tuple[float, tuple[int, int]]:
    try:
        with open("/proc/stat") as fh:
            parts = [int(x) for x in fh.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        pidle, ptotal = prev
        dt = total - ptotal
        pct = 0.0 if dt <= 0 else (1 - (idle - pidle) / dt) * 100
        return max(0.0, min(100.0, pct)), (idle, total)
    except OSError:
        return 0.0, prev


def _nvidia_smi() -> tuple[bool, str, int, int]:
    if not shutil.which("nvidia-smi"):
        return False, "", 0, 0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        first = out.stdout.strip().splitlines()[0]
        name, used, total = [p.strip() for p in first.split(",")]
        return True, name, int(float(used)), int(float(total))
    except Exception:
        return False, "", 0, 0


class ResourceMonitor:
    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self._lock = threading.Lock()
        self._last_cpu_prev = (0, 0)
        self._current = ResourceSnapshot(0, 0, 0, 0, False, "", 0, 0, 0.0, time.time())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="resource-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample()

    def _sample(self) -> ResourceSnapshot:
        if psutil is not None:
            vm = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=None)
            total_mb, avail_mb = vm.total / 1e6, vm.available / 1e6
            try:
                load1 = psutil.getloadavg()[0]
            except (AttributeError, OSError):
                load1 = 0.0
        else:
            total_mb, avail_mb = _proc_meminfo()
            cpu, self._last_cpu_prev = _proc_cpu_percent(self._last_cpu_prev)
            try:
                load1 = time.getloadavg()[0]
            except OSError:
                load1 = 0.0
        gpu_ok, gpu_name, gu, gt = _nvidia_smi()
        ram_pct = (1 - avail_mb / total_mb) * 100 if total_mb else 0.0
        pressure = min(1.0, 0.5 * (cpu / 100.0) + 0.5 * (ram_pct / 100.0))
        snap = ResourceSnapshot(round(cpu, 1), round(total_mb), round(avail_mb),
                                round(ram_pct, 1), gpu_ok, gpu_name, gu, gt,
                                round(pressure, 3), time.time())
        with self._lock:
            self._current = snap
        return snap

    def snapshot(self) -> ResourceSnapshot:
        with self._lock:
            cur = self._current
        if time.time() - cur.sampled_at > self.interval * 2:
            cur = self._sample()
        return cur

    def recommend_local_only(self) -> bool:
        """True when the box is too loaded/low-memory for cloud streaming."""
        s = self.snapshot()
        return s.load_pressure > 0.92 or s.ram_available_mb < 300


resource_monitor = ResourceMonitor()
