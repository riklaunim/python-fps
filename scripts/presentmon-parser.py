"""
PresentMon 2.x Real-Time FPS Parser (Windows)
===============================================
Launches PresentMon Console Application targeting a specific process,
streams CSV output from stdout, and computes rolling FPS + frame time stats.

Requirements:
  - PresentMon 2.x installed (https://github.com/GameTechDev/PresentMon)
  - Target game/app already running
  - Run as Administrator (PresentMon needs ETW access)

Usage:
  python presentmon_parser.py --process "game.exe"
  python presentmon_parser.py --process "game.exe" --duration 60 --window 1.0
  python presentmon_parser.py --process "game.exe" --presentmon "D:\\Path\\To\\PresentMon.exe"
  python presentmon_parser.py --process "game.exe" --save capture.csv
"""

import argparse
import csv
import ctypes
import io
import shutil
import subprocess
import sys
import time
import statistics
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


# ── Admin check ──────────────────────────────────────────────────

def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


# ── Locate PresentMon ────────────────────────────────────────────

DEFAULT_PATHS = [
    r"C:\Program Files\Intel\PresentMon\PresentMonConsoleApplication\PresentMon-2.4.1-x64.exe",
    r"C:\Program Files\Intel\PresentMon\PresentMonConsoleApplication\PresentMon-2.3.0-x64.exe",
    r"C:\Program Files\Intel\PresentMon\PresentMonConsoleApplication\PresentMon-2.2.0-x64.exe",
]


def find_presentmon(custom_path: str | None = None) -> str:
    if custom_path:
        p = Path(custom_path)
        if p.exists():
            return str(p)
        print(f"[!] Specified path not found: {custom_path}")
        sys.exit(1)

    for candidate in DEFAULT_PATHS:
        if Path(candidate).exists():
            return candidate

    found = shutil.which("PresentMon")
    if found:
        return found

    print("[!] PresentMon not found. Install it or specify path with --presentmon")
    print("    Download: https://github.com/GameTechDev/PresentMon/releases")
    sys.exit(1)


# ── Data structures ──────────────────────────────────────────────

@dataclass
class FrameSample:
    timestamp: float
    frame_time_ms: float
    display_latency_ms: float
    gpu_busy_ms: float


@dataclass
class RollingStats:
    window_seconds: float = 1.0
    samples: deque = field(default_factory=deque)

    def add(self, sample: FrameSample):
        self.samples.append(sample)
        cutoff = sample.timestamp - self.window_seconds
        while self.samples and self.samples[0].timestamp < cutoff:
            self.samples.popleft()

    @property
    def fps(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        span = self.samples[-1].timestamp - self.samples[0].timestamp
        return (len(self.samples) - 1) / span if span > 0 else 0.0

    @property
    def avg_frame_time(self) -> float:
        return statistics.mean(s.frame_time_ms for s in self.samples) if self.samples else 0.0

    @property
    def p99_frame_time(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        ft = sorted(s.frame_time_ms for s in self.samples)
        return ft[min(int(len(ft) * 0.99), len(ft) - 1)]

    @property
    def p1_fps(self) -> float:
        p99 = self.p99_frame_time
        return (1000.0 / p99) if p99 > 0 else 0.0


def safe_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return default


# ── CSV column detection ─────────────────────────────────────────

COLUMN_ALIASES = {
    "timestamp": ["TimeInMs", "TimeInSeconds", "TimeinSeconds"],
    "frametime": ["MsBetweenPresents", "msBetweenPresents"],
    "display":   ["MsBetweenDisplayChange", "msBetweenDisplayChange",
                  "MsDisplayLatency", "msDisplayLatency"],
    "gpu":       ["MsGPUBusy", "msGPUBusy", "GPUBusy"],
}

# Columns where the value is in ms and needs converting to seconds for internal use
MS_TIMESTAMP_COLUMNS = {"TimeInMs"}


def resolve_columns(headers: list[str]) -> tuple[dict[str, int | None], bool]:
    """Returns (column_map, timestamp_is_ms)."""
    header_map = {h.strip(): i for i, h in enumerate(headers)}
    resolved = {}
    ts_is_ms = False
    for key, aliases in COLUMN_ALIASES.items():
        resolved[key] = None
        for alias in aliases:
            if alias in header_map:
                resolved[key] = header_map[alias]
                if key == "timestamp" and alias in MS_TIMESTAMP_COLUMNS:
                    ts_is_ms = True
                break
    return resolved, ts_is_ms


# ── Main capture ─────────────────────────────────────────────────

def stream_presentmon(
    process_name: str,
    duration: float | None,
    window: float,
    presentmon_path: str,
    save_path: str | None,
):
    cmd = [
        presentmon_path,
        "--process_name", process_name,
        "--output_stdout",
        "--no_console_stats",
        "--terminate_on_proc_exit",
        "--stop_existing_session",
    ]
    if duration:
        cmd.extend(["--timed", str(int(duration)),
                     "--terminate_after_timed"])

    print(f"[*] PresentMon: {presentmon_path}")
    print(f"[*] Target:     {process_name}")
    print(f"[*] Command:    {' '.join(cmd)}")
    print(f"[*] Window:     {window}s\n")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # Line-buffered
    )

    # Brief pause to check for immediate failure
    time.sleep(0.5)
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        print(f"[!] PresentMon exited immediately (code {proc.returncode})")
        if stderr:
            print(f"    stderr: {stderr.strip()}")
        return

    # Optional: save raw CSV alongside live display
    save_file = None
    if save_path:
        save_file = open(save_path, "w", newline="")

    stats = RollingStats(window_seconds=window)
    columns: dict[str, int | None] | None = None
    frame_count = 0
    start_time = time.monotonic()
    last_print = 0.0

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            # Save raw CSV data
            if save_file and columns is not None:
                save_file.write(line + "\n")
            elif save_file and columns is None:
                # Write header
                save_file.write(line + "\n")

            # Parse CSV header
            if columns is None:
                reader = csv.reader(io.StringIO(line))
                headers = next(reader)
                columns, ts_is_ms = resolve_columns(headers)

                if columns["timestamp"] is None or columns["frametime"] is None:
                    print("[!] Required columns not found in PresentMon output.")
                    print(f"    Available: {[h.strip() for h in headers]}")
                    break

                found = {k: v for k, v in columns.items() if v is not None}
                ts_unit = "ms" if ts_is_ms else "s"
                print(f"[*] Columns: {found} (timestamp unit: {ts_unit})\n")
                print(
                    f"{'Time':>8s}  {'FPS':>7s}  {'Avg ms':>7s}  "
                    f"{'P99 ms':>7s}  {'1% Low':>7s}"
                )
                print("-" * 48)
                continue

            # Parse data row
            reader = csv.reader(io.StringIO(line))
            try:
                fields = next(reader)
            except StopIteration:
                continue

            raw_ts = safe_float(fields[columns["timestamp"]])
            sample = FrameSample(
                timestamp=raw_ts / 1000.0 if ts_is_ms else raw_ts,
                frame_time_ms=safe_float(fields[columns["frametime"]]),
                display_latency_ms=(
                    safe_float(fields[columns["display"]])
                    if columns["display"] else 0.0
                ),
                gpu_busy_ms=(
                    safe_float(fields[columns["gpu"]])
                    if columns["gpu"] else 0.0
                ),
            )

            if sample.frame_time_ms <= 0:
                continue

            stats.add(sample)
            frame_count += 1

            # Print at ~2 Hz
            now = time.monotonic()
            if now - last_print >= 0.5:
                elapsed = now - start_time
                print(
                    f"{elapsed:>7.1f}s  "
                    f"{stats.fps:>7.1f}  "
                    f"{stats.avg_frame_time:>7.2f}  "
                    f"{stats.p99_frame_time:>7.2f}  "
                    f"{stats.p1_fps:>7.1f}"
                )
                last_print = now

    except KeyboardInterrupt:
        print("\n[*] Interrupted by user.")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if save_file:
            save_file.close()

    # Print any stderr (warnings, errors)
    stderr = proc.stderr.read() if proc.stderr else ""
    if stderr.strip():
        print(f"\n[*] PresentMon stderr:\n    {stderr.strip()}")

    # Summary
    elapsed = time.monotonic() - start_time
    print(f"\n{'='*48}")
    print(f"Total frames captured: {frame_count}")
    print(f"Session duration:      {elapsed:.1f}s")
    if stats.samples:
        all_ft = [s.frame_time_ms for s in stats.samples]
        print(f"Final window FPS:      {stats.fps:.1f}")
        print(f"Avg frame time:        {stats.avg_frame_time:.2f} ms")
        print(f"Min frame time:        {min(all_ft):.2f} ms")
        print(f"Max frame time:        {max(all_ft):.2f} ms")
        print(f"1% Low FPS:            {stats.p1_fps:.1f}")
    if save_path:
        print(f"Raw CSV saved:         {save_path}")


def main():
    parser = argparse.ArgumentParser(
        description="PresentMon 2.x real-time FPS monitor"
    )
    parser.add_argument(
        "--process", "-p", required=True,
        help="Target process name (e.g., 'game.exe')",
    )
    parser.add_argument(
        "--duration", "-d", type=float, default=None,
        help="Recording duration in seconds",
    )
    parser.add_argument(
        "--window", "-w", type=float, default=1.0,
        help="Rolling stats window in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--presentmon", type=str, default=None,
        help="Path to PresentMon console executable",
    )
    parser.add_argument(
        "--save", "-s", type=str, default=None,
        help="Save raw CSV to this path (in addition to live display)",
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        print("[!] PresentMon is Windows-only. Use MangoHud on Linux.")
        sys.exit(1)

    if not is_admin():
        print("[!] PresentMon requires Administrator privileges (ETW access).")
        print("    Right-click your terminal and 'Run as administrator'.")
        sys.exit(1)

    presentmon = find_presentmon(args.presentmon)
    print(f"[*] Found PresentMon: {presentmon}\n")

    stream_presentmon(
        args.process, args.duration, args.window, presentmon, args.save,
    )


if __name__ == "__main__":
    main()
