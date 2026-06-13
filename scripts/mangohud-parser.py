"""
MangoHud Real-Time & Log FPS Parser (Linux)
=============================================
Two modes:
  1. LAUNCH mode: Launches a game with MangoHud enabled, captures logs in real time.
  2. PARSE mode: Parses an existing MangoHud CSV log file.

MangoHud writes CSV logs with columns like:
  fps, frametime, cpu_load, gpu_load, cpu_temp, gpu_temp, cpu_power, gpu_power, ...

Requirements:
  - MangoHud installed (https://github.com/flightlessmango/MangoHud)
  - Vulkan or OpenGL game

Usage:
  # Launch a game and monitor in real time
  python mangohud-parser.py launch -- gamescope -f -- steam -gamepadui

  # Parse an existing MangoHud log
  python mangohud-parser.py parse /path/to/MangoHud_2024-01-15_12-30-00.csv

  # Watch a log directory for new files
  python mangohud-parser.py watch ~/.local/share/MangoHud/
"""

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
import statistics
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


# ── Data Structures ──────────────────────────────────────────────

@dataclass
class MangoSample:
    timestamp: float        # Wall-clock time (monotonic, seconds)
    fps: float
    frametime_ms: float
    cpu_load: float         # Percent
    gpu_load: float         # Percent
    cpu_temp: float         # Celsius
    gpu_temp: float         # Celsius
    ram_used_mb: float
    vram_used_mb: float


@dataclass
class RollingWindow:
    window_sec: float = 2.0
    samples: deque = field(default_factory=deque)

    def add(self, s: MangoSample):
        self.samples.append(s)
        cutoff = s.timestamp - self.window_sec
        while self.samples and self.samples[0].timestamp < cutoff:
            self.samples.popleft()

    def _ft_list(self):
        return [s.frametime_ms for s in self.samples if s.frametime_ms > 0]

    @property
    def avg_fps(self) -> float:
        if not self.samples:
            return 0.0
        return statistics.mean(s.fps for s in self.samples)

    @property
    def avg_frametime(self) -> float:
        ft = self._ft_list()
        return statistics.mean(ft) if ft else 0.0

    @property
    def p1_low_fps(self) -> float:
        ft = self._ft_list()
        if len(ft) < 10:
            return 0.0
        ft_sorted = sorted(ft, reverse=True)
        p99_ft = ft_sorted[max(0, int(len(ft_sorted) * 0.01))]
        return 1000.0 / p99_ft if p99_ft > 0 else 0.0

    @property
    def avg_cpu(self) -> float:
        vals = [s.cpu_load for s in self.samples if s.cpu_load >= 0]
        return statistics.mean(vals) if vals else 0.0

    @property
    def avg_gpu(self) -> float:
        vals = [s.gpu_load for s in self.samples if s.gpu_load >= 0]
        return statistics.mean(vals) if vals else 0.0


def safe_float(val: str, default: float = 0.0) -> float:
    try:
        return float(val.strip())
    except (ValueError, AttributeError):
        return default


# ── Parsing ──────────────────────────────────────────────────────

def parse_mangohud_row(row: dict, timestamp: float) -> MangoSample:
    """Parse a single CSV dict-row into a MangoSample."""
    return MangoSample(
        timestamp=timestamp,
        fps=safe_float(row.get("fps", "")),
        frametime_ms=safe_float(row.get("frametime", "")),
        cpu_load=safe_float(row.get("cpu_load", "")),
        gpu_load=safe_float(row.get("gpu_load", "")),
        cpu_temp=safe_float(row.get("cpu_temp", "")),
        gpu_temp=safe_float(row.get("gpu_temp", "")),
        ram_used_mb=safe_float(row.get("ram_used", "")),
        vram_used_mb=safe_float(row.get("vram_used", "")),
    )


def print_header():
    print(
        f"{'Time':>7s}  {'FPS':>6s}  {'FT ms':>6s}  "
        f"{'1%Low':>6s}  {'CPU%':>5s}  {'GPU%':>5s}  "
        f"{'C°':>4s}  {'G°':>4s}"
    )
    print("-" * 62)


def print_stats(elapsed: float, w: RollingWindow):
    s = w.samples[-1] if w.samples else None
    print(
        f"{elapsed:>6.1f}s  {w.avg_fps:>6.1f}  {w.avg_frametime:>6.2f}  "
        f"{w.p1_low_fps:>6.1f}  {w.avg_cpu:>5.1f}  {w.avg_gpu:>5.1f}  "
        f"{(s.cpu_temp if s else 0):>4.0f}  {(s.gpu_temp if s else 0):>4.0f}"
    )


# ── Mode: Parse existing log ────────────────────────────────────

def parse_log(filepath: str):
    """Parse a complete MangoHud CSV log and print summary statistics."""
    path = Path(filepath)
    if not path.exists():
        print(f"[!] File not found: {path}")
        sys.exit(1)

    all_fps = []
    all_ft = []
    all_cpu = []
    all_gpu = []

    with open(path, newline="") as f:
        raw_lines = [ln for ln in f if not ln.startswith("#") and ln.strip()]

    # MangoHud logs start with two metadata lines (system info header +
    # values) before the actual data header containing "fps", "frametime", etc.
    # Find the real header by looking for a line that contains known data columns.
    data_header_idx = 0
    for i, ln in enumerate(raw_lines):
        fields = [f.strip().lower() for f in ln.split(",")]
        if "fps" in fields or "frametime" in fields:
            data_header_idx = i
            break

    if data_header_idx > 0:
        skipped = raw_lines[:data_header_idx]
        print(f"  (skipped {data_header_idx} metadata line(s))")
        # Optionally show system info from metadata
        if len(skipped) >= 2:
            meta_keys = [k.strip() for k in skipped[0].split(",")]
            meta_vals = [v.strip() for v in skipped[1].split(",")]
            meta = dict(zip(meta_keys, meta_vals))
            for key in ["os", "cpu", "gpu", "ram", "kernel", "driver"]:
                if key in meta and meta[key]:
                    print(f"  {key:>8s}: {meta[key]}")
            print()

    lines = raw_lines[data_header_idx:]
    reader = csv.DictReader(lines)
    for row in reader:
        fps = safe_float(row.get("fps", ""))
        ft = safe_float(row.get("frametime", ""))
        if fps > 0:
            all_fps.append(fps)
        if ft > 0:
            all_ft.append(ft)
        cpu = safe_float(row.get("cpu_load", ""))
        gpu = safe_float(row.get("gpu_load", ""))
        if cpu >= 0:
            all_cpu.append(cpu)
        if gpu >= 0:
            all_gpu.append(gpu)

    if not all_fps:
        print("[!] No valid FPS data found in log.")
        return

    all_ft_sorted = sorted(all_ft)
    all_fps_sorted = sorted(all_fps)

    print(f"MangoHud Log Analysis: {path.name}")
    print("=" * 50)
    print(f"  Total samples:     {len(all_fps)}")
    duration_est = sum(all_ft) / 1000.0
    print(f"  Est. duration:     {duration_est:.1f}s")
    print()
    print(f"  Avg FPS:           {statistics.mean(all_fps):.1f}")
    print(f"  Median FPS:        {statistics.median(all_fps):.1f}")
    print(f"  Min FPS:           {min(all_fps):.1f}")
    print(f"  Max FPS:           {max(all_fps):.1f}")
    print(f"  Std Dev FPS:       {statistics.stdev(all_fps):.1f}" if len(all_fps) > 1 else "")
    print()
    print(f"  Avg frame time:    {statistics.mean(all_ft):.2f} ms")
    print(f"  P95 frame time:    {all_ft_sorted[int(len(all_ft_sorted) * 0.95)]:.2f} ms")
    print(f"  P99 frame time:    {all_ft_sorted[int(len(all_ft_sorted) * 0.99)]:.2f} ms")
    print(f"  Max frame time:    {max(all_ft):.2f} ms")
    print()
    p1_low = all_fps_sorted[max(0, int(len(all_fps_sorted) * 0.01))]
    p01_low = all_fps_sorted[max(0, int(len(all_fps_sorted) * 0.001))]
    print(f"  1% Low FPS:        {p1_low:.1f}")
    print(f"  0.1% Low FPS:      {p01_low:.1f}")
    if all_cpu:
        print(f"\n  Avg CPU load:      {statistics.mean(all_cpu):.1f}%")
    if all_gpu:
        print(f"  Avg GPU load:      {statistics.mean(all_gpu):.1f}%")


# ── Mode: Launch game with MangoHud ─────────────────────────────

def launch_and_monitor(game_cmd: list[str], window: float):
    """
    Launch a game with MangoHud logging enabled and stream stats.

    Key environment variables:
      MANGOHUD=1              Enable the overlay
      MANGOHUD_LOG=1          Enable CSV logging
      MANGOHUD_OUTPUT=<dir>   Where to write logs
    """
    log_dir = Path.home() / ".local" / "share" / "MangoHud"
    log_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update({
        "MANGOHUD": "1",
        "MANGOHUD_LOG": "1",
        "MANGOHUD_OUTPUT": str(log_dir),
        # Optional: configure what MangoHud logs
        "MANGOHUD_CONFIG": "fps,frametime,cpu_load,gpu_load,cpu_temp,gpu_temp,ram,vram,log_interval=0",
    })

    # Prepend mangohud to the command
    full_cmd = ["mangohud"] + game_cmd

    print(f"[*] Launching: {' '.join(full_cmd)}")
    print(f"[*] Log directory: {log_dir}")
    print(f"[*] Waiting for MangoHud log file...\n")

    proc = subprocess.Popen(full_cmd, env=env)

    # Wait for a new log file to appear
    existing_logs = set(log_dir.glob("MangoHud_*.csv"))
    new_log = None
    timeout = 30
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        current_logs = set(log_dir.glob("MangoHud_*.csv"))
        new_files = current_logs - existing_logs
        if new_files:
            new_log = max(new_files, key=lambda p: p.stat().st_mtime)
            break
        time.sleep(0.5)

    if not new_log:
        print("[!] Timed out waiting for MangoHud log. Is MangoHud working?")
        proc.terminate()
        return

    print(f"[*] Tailing: {new_log.name}\n")

    # Tail the log file
    rolling = RollingWindow(window_sec=window)
    header = None
    metadata_lines_seen = 0
    last_print = 0
    file_pos = 0
    t0 = time.monotonic()

    try:
        while proc.poll() is None:
            with open(new_log) as f:
                f.seek(file_pos)
                new_data = f.read()
                file_pos = f.tell()

            if not new_data:
                time.sleep(0.1)
                continue

            for line in new_data.strip().split("\n"):
                if line.startswith("#") or not line.strip():
                    continue

                # Skip metadata lines before the real data header.
                # The real header contains "fps" or "frametime".
                if header is None:
                    fields_lower = [f.strip().lower() for f in line.split(",")]
                    if "fps" not in fields_lower and "frametime" not in fields_lower:
                        metadata_lines_seen += 1
                        continue
                    header = [h.strip() for h in line.strip().split(",")]
                    print_header()
                    continue

                values = line.strip().split(",")
                row = dict(zip(header, values))
                elapsed = time.monotonic() - t0
                sample = parse_mangohud_row(row, elapsed)
                rolling.add(sample)

                # Print roughly every 0.5s
                if elapsed - last_print >= 0.5:
                    print_stats(elapsed, rolling)
                    last_print = elapsed

    except KeyboardInterrupt:
        print("\n[*] Stopping...")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n[*] Log saved at: {new_log}")
    print("[*] Run `python mangohud_parser.py parse <logfile>` for full analysis.")


# ── Mode: Watch directory ────────────────────────────────────────

def watch_directory(dirpath: str):
    """Watch for new MangoHud logs and auto-parse them when complete."""
    log_dir = Path(dirpath)
    if not log_dir.is_dir():
        print(f"[!] Not a directory: {log_dir}")
        sys.exit(1)

    known = set(log_dir.glob("MangoHud_*.csv"))
    print(f"[*] Watching {log_dir} for new MangoHud logs (Ctrl+C to stop)...")

    try:
        while True:
            current = set(log_dir.glob("MangoHud_*.csv"))
            new_files = current - known
            for nf in sorted(new_files):
                # Wait a moment for the file to finish writing
                time.sleep(2)
                print(f"\n[+] New log detected: {nf.name}")
                parse_log(str(nf))
                known.add(nf)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Stopped watching.")


# ── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MangoHud FPS log tool")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_launch = sub.add_parser("launch", help="Launch game with MangoHud and monitor")
    p_launch.add_argument("--window", "-w", type=float, default=2.0, help="Rolling window (seconds)")
    p_launch.add_argument("command", nargs=argparse.REMAINDER, help="Game command (after --)")

    p_parse = sub.add_parser("parse", help="Parse an existing MangoHud log file")
    p_parse.add_argument("logfile", help="Path to MangoHud CSV log")

    p_watch = sub.add_parser("watch", help="Watch a directory for new MangoHud logs")
    p_watch.add_argument("directory", nargs="?", default=str(Path.home() / ".local/share/MangoHud"))

    args = parser.parse_args()

    if args.mode == "launch":
        cmd = args.command
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("[!] Provide a game command after '--', e.g.:")
            print("    python mangohud_parser.py launch -- steam -gamepadui")
            sys.exit(1)
        launch_and_monitor(cmd, args.window)

    elif args.mode == "parse":
        parse_log(args.logfile)

    elif args.mode == "watch":
        watch_directory(args.directory)


if __name__ == "__main__":
    main()
