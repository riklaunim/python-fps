"""
RTSS (RivaTuner Statistics Server) Shared Memory Reader (Windows)
=================================================================
Reads real-time FPS and frame time directly from RTSS's shared memory
using raw byte offsets discovered via the diagnostic script.

Uses struct.unpack_from instead of ctypes.Structure to avoid layout
mismatches across RTSS versions.

Requirements:
  - RTSS running (standalone or via MSI Afterburner)
  - A hooked 3D application (game) running
  - Windows only

Usage:
  python rtss_reader.py
  python rtss_reader.py --interval 0.5 --duration 120
  python rtss_reader.py --csv output.csv

If field offsets don't match your RTSS version, run rtss_diagnostic.py
and update the OFFSET_* constants below.
"""

import argparse
import ctypes
import ctypes.wintypes
import csv
import struct
import sys
import time
import statistics
from collections import deque
from dataclasses import dataclass, field


# ── Field offsets within an app entry ────────────────────────────
# Discovered via rtss_diagnostic.py against RTSS 7.3.7.
# If your version differs, re-run the diagnostic and update these.

OFFSET_PID    = 0        # DWORD  dwProcessID
OFFSET_NAME   = 4        # CHAR[] szName (null-terminated, up to MAX_PATH)
NAME_MAX_LEN  = 260      # Read up to MAX_PATH bytes for the name
OFFSET_FLAGS  = 264      # DWORD  dwFlags
OFFSET_TIME0  = 268      # DWORD  dwTime0  (ms, start of measurement window)
OFFSET_TIME1  = 272      # DWORD  dwTime1  (ms, end of measurement window)
OFFSET_FRAMES = 276      # DWORD  dwFrames (total rendered frames)

# ── Shared memory / header offsets ───────────────────────────────

RTSS_SHM_NAME = "RTSSSharedMemoryV2"
FILE_MAP_READ = 0x0004

# Header field offsets (these are stable across versions)
HDR_SIGNATURE       = 0   # 4 bytes
HDR_VERSION         = 4   # DWORD
HDR_APP_ENTRY_SIZE  = 8   # DWORD
HDR_APP_ARR_OFFSET  = 12  # DWORD
HDR_APP_ARR_SIZE    = 16  # DWORD


# ── Windows API setup ────────────────────────────────────────────

if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.OpenFileMappingW.restype = ctypes.wintypes.HANDLE
    kernel32.OpenFileMappingW.argtypes = [
        ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR,
    ]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p
    kernel32.MapViewOfFile.argtypes = [
        ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.c_size_t,
    ]
    kernel32.UnmapViewOfFile.restype = ctypes.wintypes.BOOL
    kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
else:
    kernel32 = None


# ── Raw memory helpers ───────────────────────────────────────────

def read_u32(base: int, offset: int) -> int:
    raw = ctypes.string_at(base + offset, 4)
    return struct.unpack_from("<I", raw)[0]


def read_str(base: int, offset: int, max_len: int) -> str:
    raw = ctypes.string_at(base + offset, max_len)
    null = raw.find(b"\x00")
    if null >= 0:
        raw = raw[:null]
    return raw.decode("utf-8", errors="replace")


# ── Shared memory open / close ───────────────────────────────────

def open_shared_memory() -> tuple[int, int]:
    """Returns (handle, base_address)."""
    h = kernel32.OpenFileMappingW(FILE_MAP_READ, False, RTSS_SHM_NAME)
    if not h:
        raise OSError(
            "Could not open RTSS shared memory. Is RTSS running and a game hooked?"
        )
    p = kernel32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
    if not p:
        kernel32.CloseHandle(h)
        raise OSError("Could not map RTSS shared memory.")
    return h, p


def close_shared_memory(h: int, p: int):
    kernel32.UnmapViewOfFile(p)
    kernel32.CloseHandle(h)


# ── Stats ────────────────────────────────────────────────────────

@dataclass
class FrameStats:
    timestamp: float
    process_name: str
    pid: int
    fps: float
    frametime_ms: float
    total_frames: int


@dataclass
class PrevSnapshot:
    dwTime1: int = 0
    dwFrames: int = 0


@dataclass
class RollingStats:
    window_sec: float = 2.0
    samples: deque = field(default_factory=deque)

    def add(self, s: FrameStats):
        self.samples.append(s)
        cutoff = s.timestamp - self.window_sec
        while self.samples and self.samples[0].timestamp < cutoff:
            self.samples.popleft()

    @property
    def avg_fps(self) -> float:
        return statistics.mean(s.fps for s in self.samples) if self.samples else 0.0

    @property
    def min_fps(self) -> float:
        return min(s.fps for s in self.samples) if self.samples else 0.0

    @property
    def avg_frametime(self) -> float:
        return statistics.mean(s.frametime_ms for s in self.samples) if self.samples else 0.0

    @property
    def max_frametime(self) -> float:
        return max(s.frametime_ms for s in self.samples) if self.samples else 0.0


# No per-PID state needed — FPS is read directly from the current window


def read_entry_stats(base: int, entry_base: int) -> FrameStats | None:
    """
    Read FPS directly from RTSS's current measurement window.

    RTSS maintains a sliding window defined by dwTime0/dwTime1 (ms timestamps)
    and dwFrames (frames rendered in that window). The OSD displays:

        FPS = dwFrames * 1000 / (dwTime1 - dwTime0)

    This is a direct read with no delta computation, so it's immune to
    window resets and doesn't need cross-poll state.
    """
    pid = read_u32(entry_base, OFFSET_PID)
    if pid == 0:
        return None

    time0  = read_u32(entry_base, OFFSET_TIME0)
    time1  = read_u32(entry_base, OFFSET_TIME1)
    frames = read_u32(entry_base, OFFSET_FRAMES)

    dt_ms = time1 - time0
    if dt_ms <= 0 or frames == 0:
        return None

    fps = frames * 1000.0 / dt_ms
    frametime_ms = dt_ms / frames
    name = read_str(entry_base, OFFSET_NAME, NAME_MAX_LEN)

    return FrameStats(
        timestamp=time.monotonic(),
        process_name=name,
        pid=pid,
        fps=fps,
        frametime_ms=frametime_ms,
        total_frames=frames,
    )


# ── Main loop ────────────────────────────────────────────────────

def monitor(interval: float, duration: float | None, csv_path: str | None):
    print("[*] Connecting to RTSS shared memory...")

    try:
        hMap, base = open_shared_memory()
    except OSError as e:
        print(f"[!] {e}")
        sys.exit(1)

    sig = ctypes.string_at(base, 4)
    version = read_u32(base, HDR_VERSION)
    entry_size = read_u32(base, HDR_APP_ENTRY_SIZE)
    arr_offset = read_u32(base, HDR_APP_ARR_OFFSET)
    arr_count = read_u32(base, HDR_APP_ARR_SIZE)

    print(f"[*] Connected — Signature: {sig}, Version: 0x{version:08X}")
    print(f"[*] Entry size: {entry_size} bytes, Slots: {arr_count}")
    print(f"[*] Using offsets: TIME1={OFFSET_TIME1}, FRAMES={OFFSET_FRAMES}\n")

    csv_file = None
    csv_writer = None
    if csv_path:
        csv_file = open(csv_path, "w", newline="")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["timestamp", "process", "pid", "fps", "frametime_ms", "total_frames"])

    rolling = RollingStats(window_sec=2.0)
    t0 = time.monotonic()

    print(
        f"{'Time':>7s}  {'Process':<30s}  {'FPS':>7s}  "
        f"{'FT ms':>7s}  {'RollFPS':>8s}  {'RollFT':>7s}"
    )
    print("-" * 80)

    try:
        while True:
            elapsed = time.monotonic() - t0
            if duration and elapsed > duration:
                break

            # Re-read header in case apps changed
            current_count = read_u32(base, HDR_APP_ARR_SIZE)

            for i in range(current_count):
                entry_base = base + arr_offset + i * entry_size
                stats = read_entry_stats(base, entry_base)
                if stats is None:
                    continue

                rolling.add(stats)

                # Truncate long paths for display
                display_name = stats.process_name
                if len(display_name) > 30:
                    display_name = "..." + display_name[-(30 - 3):]

                print(
                    f"{elapsed:>6.1f}s  {display_name:<30s}  "
                    f"{stats.fps:>7.1f}  "
                    f"{stats.frametime_ms:>7.2f}  "
                    f"{rolling.avg_fps:>8.1f}  "
                    f"{rolling.avg_frametime:>7.2f}"
                )

                if csv_writer:
                    csv_writer.writerow([
                        f"{elapsed:.3f}", stats.process_name, stats.pid,
                        f"{stats.fps:.1f}", f"{stats.frametime_ms:.3f}",
                        stats.total_frames,
                    ])

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n[*] Stopped.")
    finally:
        close_shared_memory(hMap, base)
        if csv_file:
            csv_file.close()
            print(f"[*] CSV saved: {csv_path}")

    if rolling.samples:
        print(f"\n{'='*60}")
        print(f"  Session duration:  {time.monotonic() - t0:.1f}s")
        print(f"  Avg FPS:           {rolling.avg_fps:.1f}")
        print(f"  Min FPS:           {rolling.min_fps:.1f}")
        print(f"  Avg frame time:    {rolling.avg_frametime:.2f} ms")
        print(f"  Max frame time:    {rolling.max_frametime:.2f} ms")


def list_hooked_apps():
    try:
        hMap, base = open_shared_memory()
    except OSError as e:
        print(f"[!] {e}")
        return

    entry_size = read_u32(base, HDR_APP_ENTRY_SIZE)
    arr_offset = read_u32(base, HDR_APP_ARR_OFFSET)
    arr_count = read_u32(base, HDR_APP_ARR_SIZE)

    print(f"RTSS Hooked Applications ({arr_count} slots):")
    print("-" * 60)

    found = False
    for i in range(arr_count):
        entry_base = base + arr_offset + i * entry_size
        pid = read_u32(entry_base, OFFSET_PID)
        if pid != 0:
            name = read_str(entry_base, OFFSET_NAME, NAME_MAX_LEN)
            frames = read_u32(entry_base, OFFSET_FRAMES)
            time0 = read_u32(entry_base, OFFSET_TIME0)
            time1 = read_u32(entry_base, OFFSET_TIME1)
            dt = time1 - time0
            fps = (frames * 1000.0 / dt) if dt > 0 else 0
            short_name = name if len(name) <= 40 else "..." + name[-37:]
            print(f"  [{i}] PID {pid:>6d}  {short_name:<40s}  "
                  f"Frames: {frames:>8d}  AvgFPS: {fps:.1f}")
            found = True

    if not found:
        print("  (no hooked applications found)")

    close_shared_memory(hMap, base)


def main():
    parser = argparse.ArgumentParser(description="RTSS Shared Memory FPS Reader")
    parser.add_argument("--interval", "-i", type=float, default=1.0,
                        help="Polling interval in seconds (default: 1.0)")
    parser.add_argument("--duration", "-d", type=float, default=None,
                        help="Recording duration in seconds")
    parser.add_argument("--csv", "-c", type=str, default=None,
                        help="Output CSV file path")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List hooked applications and exit")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("[!] RTSS is Windows-only.")
        sys.exit(1)

    if args.list:
        list_hooked_apps()
    else:
        monitor(args.interval, args.duration, args.csv)


if __name__ == "__main__":
    main()
