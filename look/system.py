"""Read-only facts about the machine, for `lk machine`, LO's inspection tools and
Future Crash's telemetry panel. macOS and Linux; each probe degrades to None."""
from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def run(cmd: List[str], timeout: float = 3.0) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def human(n: Optional[float]) -> str:
    if n is None:
        return "?"
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024 or unit == "P":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return f"{d}d {h:02d}h {m:02d}m" if d else f"{h}h {m:02d}m"


# ---- samplers -----------------------------------------------------------

class Telemetry:
    """Cheap incremental samples: CPU and network need deltas between calls."""

    def __init__(self):
        self._cpu_prev: Optional[Tuple[int, int]] = None
        self._net_prev: Optional[Tuple[int, int, float]] = None

    def cpu(self) -> float:
        if sys.platform == "darwin":
            out = run(["top", "-l", "1", "-n", "0"], 3.0)
            m = re.search(r"CPU usage:.*?([\d.]+)% idle", out)
            return max(0.0, min(1.0, (100 - float(m.group(1))) / 100)) if m else 0.0
        try:
            vals = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        except (OSError, ValueError, IndexError):
            return 0.0
        idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
        value = 0.0
        if self._cpu_prev and total != self._cpu_prev[1]:
            value = 1 - (idle - self._cpu_prev[0]) / (total - self._cpu_prev[1])
        self._cpu_prev = (idle, total)
        return max(0.0, min(1.0, value))

    def net(self) -> Tuple[float, float]:
        rx = tx = 0
        if sys.platform == "darwin":
            seen = set()
            for line in run(["netstat", "-ib"]).splitlines()[1:]:
                parts = line.split()
                if len(parts) < 10 or parts[0].startswith("lo") or parts[0] in seen:
                    continue
                nums = [int(t) for t in parts if t.isdigit()]
                if len(nums) >= 2:
                    rx, tx = rx + nums[-2], tx + nums[-1]
                    seen.add(parts[0])
        else:
            try:
                for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
                    iface, rest = line.split(":", 1)
                    if iface.strip() != "lo":
                        p = rest.split()
                        rx, tx = rx + int(p[0]), tx + int(p[8])
            except (OSError, ValueError, IndexError):
                pass
        now = time.time()
        rate = (0.0, 0.0)
        if self._net_prev:
            dt = max(0.001, now - self._net_prev[2])
            rate = (max(0.0, (rx - self._net_prev[0]) / dt), max(0.0, (tx - self._net_prev[1]) / dt))
        self._net_prev = (rx, tx, now)
        return rate


def memory() -> Tuple[int, int]:
    """(used, total) bytes."""
    if sys.platform == "darwin":
        try:
            total = int(run(["sysctl", "-n", "hw.memsize"]).strip())
            vm = run(["vm_stat"])
            page = int(re.search(r"page size of (\d+)", vm).group(1)) if "page size of" in vm else 4096
            vals = {k.strip(): int(v.strip().rstrip(".")) for k, v in
                    (l.split(":", 1) for l in vm.splitlines()[1:] if ":" in l) if v.strip().rstrip(".").isdigit()}
            free = (vals.get("Pages free", 0) + vals.get("Pages speculative", 0) + vals.get("Pages inactive", 0)) * page
            return max(0, total - free), total
        except (ValueError, AttributeError):
            return 0, 1
    try:
        data = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            data[k] = int(v.split()[0]) * 1024
        return data["MemTotal"] - data.get("MemAvailable", 0), data["MemTotal"]
    except (OSError, KeyError, ValueError):
        return 0, 1


def uptime() -> float:
    if sys.platform == "darwin":
        m = re.search(r"sec = (\d+)", run(["sysctl", "-n", "kern.boottime"]))
        return time.time() - int(m.group(1)) if m else 0.0
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def load() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def disk(path: str = "/") -> Tuple[int, int]:
    try:
        u = shutil.disk_usage(path)
        return u.used, u.total
    except OSError:
        return 0, 1


# ---- reports ------------------------------------------------------------

def cpu_model() -> str:
    if sys.platform == "darwin":
        return run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or platform.processor()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith(("model name", "hardware")):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def machine() -> Dict[str, str]:
    used, total = memory()
    dused, dtotal = disk(str(Path.home()))
    name = platform.system()
    if name == "Darwin":
        name = "macOS " + platform.mac_ver()[0]
    else:
        try:
            pretty = next((l.split("=", 1)[1].strip('"') for l in Path("/etc/os-release").read_text().splitlines()
                           if l.startswith("PRETTY_NAME=")), "")
            name = pretty or name
        except OSError:
            pass
    return {
        "host": socket.gethostname(),
        "system": f"{name} · {platform.machine()}",
        "cpu": f"{cpu_model()} · {os.cpu_count()} cores",
        "memory": f"{human(used)} / {human(total)} ({used / max(1, total):.0%})",
        "disk": f"{human(dused)} / {human(dtotal)} ({dused / max(1, dtotal):.0%})",
        "load": f"{load():.2f}",
        "uptime": duration(uptime()),
        "python": platform.python_version(),
        "shell": os.environ.get("SHELL", "?"),
    }


def ports() -> List[Dict[str, str]]:
    rows = []
    if shutil.which("lsof"):
        for line in run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], 5).splitlines()[1:]:
            p = line.split()
            if len(p) >= 9:
                addr = p[8]
                rows.append({"port": addr.rsplit(":", 1)[-1], "address": addr, "process": p[0], "pid": p[1]})
    elif shutil.which("ss"):
        for line in run(["ss", "-ltnpH"]).splitlines():
            p = line.split()
            if len(p) >= 4:
                addr = p[3]
                proc = re.search(r'"([^"]+)",pid=(\d+)', line)
                rows.append({"port": addr.rsplit(":", 1)[-1], "address": addr,
                             "process": proc.group(1) if proc else "?", "pid": proc.group(2) if proc else "?"})
    uniq = {(r["port"], r["pid"]): r for r in rows}
    return sorted(uniq.values(), key=lambda r: int(r["port"]) if r["port"].isdigit() else 0)


def processes(query: str = "", limit: int = 25) -> List[Dict[str, str]]:
    out = run(["ps", "-axo", "pid=,pcpu=,pmem=,rss=,etime=,comm="], 5)
    rows = []
    for line in out.splitlines():
        p = line.split(None, 5)
        if len(p) < 6:
            continue
        name = p[5]
        if query and query.casefold() not in name.casefold() and query != p[0]:
            continue
        rows.append({"pid": p[0], "cpu": p[1], "mem": p[2], "rss": human(int(p[3]) * 1024) if p[3].isdigit() else p[3],
                     "elapsed": p[4], "name": name})
    rows.sort(key=lambda r: float(r["cpu"]) if r["cpu"].replace(".", "", 1).isdigit() else 0.0, reverse=True)
    return rows[:limit]


def gpu() -> str:
    if shutil.which("nvidia-smi"):
        return run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                    "--format=csv,noheader"], 5).strip()
    if sys.platform == "darwin":
        out = run(["system_profiler", "SPDisplaysDataType"], 8)
        names = re.findall(r"Chipset Model: (.+)", out)
        cores = re.findall(r"Total Number of Cores: (\d+)", out)
        return ", ".join(names) + (f" · {cores[0]} GPU cores" if cores else "")
    return ""


def git(path: Path) -> Dict[str, str]:
    if not shutil.which("git"):
        return {}
    base = ["git", "-C", str(path)]
    root = run(base + ["rev-parse", "--show-toplevel"]).strip()
    if not root:
        return {}
    return {
        "root": root,
        "branch": run(base + ["branch", "--show-current"]).strip() or "(detached)",
        "status": run(base + ["status", "--short"]).strip(),
        "recent": run(base + ["log", "--oneline", "-8"]).strip(),
        "remote": run(base + ["remote", "-v"]).strip().splitlines()[0] if run(base + ["remote"]).strip() else "",
    }


def addresses() -> List[str]:
    if sys.platform == "darwin":
        return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", run(["ifconfig"]))
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", run(["ip", "-4", "addr"]))


def which(command: str) -> Dict[str, object]:
    path = shutil.which(command)
    hits = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        c = Path(d) / command
        if c.exists() and os.access(c, os.X_OK):
            hits.append(str(c))
    return {"resolved": path, "all": hits, "real": str(Path(path).resolve()) if path else None}
