"""
RTSS Shared Memory Diagnostic v2
=================================
Scans the FULL app entry (not just first 400 bytes) to find the correct
field offsets. Polls twice and reports all changed values.

Also searches for IEEE 754 float values that might represent FPS directly.

Usage:
  python rtss-diagnostic.py
  python rtss-diagnostic.py --poll-delay 2.0
  python rtss-diagnostic.py --continuous
"""

import argparse
import ctypes
import ctypes.wintypes
import struct
import sys
import time

RTSS_SHM_NAME = "RTSSSharedMemoryV2"
FILE_MAP_READ = 0x0004

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


def open_shm():
    h = kernel32.OpenFileMappingW(FILE_MAP_READ, False, RTSS_SHM_NAME)
    if not h:
        raise OSError("Cannot open RTSS shared memory. Is RTSS running?")
    p = kernel32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
    if not p:
        kernel32.CloseHandle(h)
        raise OSError("Cannot map RTSS shared memory.")
    return h, p


def read_u32(base, offset):
    return struct.unpack_from("<I", ctypes.string_at(base + offset, 4))[0]


def read_f32(base, offset):
    return struct.unpack_from("<f", ctypes.string_at(base + offset, 4))[0]


def read_bytes(base, offset, size):
    return ctypes.string_at(base + offset, size)


def snapshot_entry(entry_base, entry_size):
    """Read the entire entry as raw bytes."""
    return ctypes.string_at(entry_base, entry_size)


def scan_and_compare(entry_base, entry_size, poll_delay):
    """Take two snapshots, compare all DWORD positions."""
    print(f"Taking snapshot 1...")
    snap1 = snapshot_entry(entry_base, entry_size)

    print(f"Waiting {poll_delay:.1f}s for snapshot 2...")
    time.sleep(poll_delay)

    snap2 = snapshot_entry(entry_base, entry_size)

    # Find name to establish baseline offset
    raw_name = snap1[4:520]
    null_pos = raw_name.find(b'\x00')
    name_str = raw_name[:null_pos].decode("utf-8", errors="replace") if null_pos > 0 else "(unknown)"
    name_end = 4 + null_pos

    pid = struct.unpack_from("<I", snap1, 0)[0]
    print(f"\nPID={pid}, Name=\"{name_str}\"")
    print(f"Name ends at byte {name_end}, entry size = {entry_size}")

    # ── Changed DWORDs (integers) ────────────────────────────────
    print(f"\n{'='*90}")
    print(f"CHANGED UINT32 VALUES")
    print(f"{'='*90}")
    print(f"{'Offset':>8s}  {'Poll 1':>14s}  {'Poll 2':>14s}  {'Delta':>12s}  Interpretation")
    print("-" * 90)

    changed_u32 = []
    for off in range(0, entry_size - 3, 4):
        # Skip name region
        if 4 <= off < 264:
            continue
        v1 = struct.unpack_from("<I", snap1, off)[0]
        v2 = struct.unpack_from("<I", snap2, off)[0]
        if v1 != v2:
            delta = v2 - v1 if v2 >= v1 else v2  # Handle wrap
            interp = ""
            expected_ms = poll_delay * 1000
            # Timestamp heuristic: delta should be ~poll_delay*1000 ms
            if expected_ms * 0.8 < delta < expected_ms * 1.2:
                interp = "⏱  likely TIMESTAMP (ms)"
            # Frame counter: plausible FPS range
            elif 10 < delta / poll_delay < 1000:
                fps_est = delta / poll_delay
                interp = f"🎮  likely FRAMES (Δ/s ≈ {fps_est:.0f} FPS)"
            elif 0 < delta <= 10:
                interp = f"(slow counter, Δ={delta})"

            changed_u32.append((off, v1, v2, delta, interp))
            print(f"  {off:>6d}    {v1:>14d}  {v2:>14d}  {delta:>+12d}  {interp}")

    # ── Changed floats ───────────────────────────────────────────
    print(f"\n{'='*90}")
    print(f"FLOAT VALUES IN PLAUSIBLE FPS RANGE (10-1000)")
    print(f"{'='*90}")
    print(f"{'Offset':>8s}  {'Poll 1':>14s}  {'Poll 2':>14s}  {'Changed':>8s}")
    print("-" * 60)

    float_candidates = []
    for off in range(0, entry_size - 3, 4):
        if 4 <= off < 264:
            continue
        f1 = struct.unpack_from("<f", snap1, off)[0]
        f2 = struct.unpack_from("<f", snap2, off)[0]
        # Check if either value is in plausible FPS range
        if (10.0 < f1 < 1000.0) or (10.0 < f2 < 1000.0):
            changed = "  YES" if abs(f1 - f2) > 0.01 else "   no"
            float_candidates.append((off, f1, f2))
            print(f"  {off:>6d}    {f1:>14.2f}  {f2:>14.2f}  {changed}")

    # ── Changed microsecond values (frametime) ───────────────────
    print(f"\n{'='*90}")
    print(f"UINT32 VALUES IN PLAUSIBLE FRAMETIME RANGE (100-100000 µs = 0.1-100 ms)")
    print(f"{'='*90}")
    print(f"{'Offset':>8s}  {'Poll 1 (µs)':>14s}  {'→ ms':>8s}  {'Poll 2 (µs)':>14s}  {'→ ms':>8s}  {'Changed':>8s}")
    print("-" * 80)

    for off in range(0, entry_size - 3, 4):
        if 4 <= off < 264:
            continue
        v1 = struct.unpack_from("<I", snap1, off)[0]
        v2 = struct.unpack_from("<I", snap2, off)[0]
        if (100 < v1 < 100000) or (100 < v2 < 100000):
            changed = "  YES" if v1 != v2 else "   no"
            print(f"  {off:>6d}    {v1:>14d}  {v1/1000:>7.2f}  {v2:>14d}  {v2/1000:>7.2f}  {changed}")

    # ── Summary ──────────────────────────────────────────────────
    print(f"\n{'='*90}")
    print("SUMMARY — Recommended offsets")
    print(f"{'='*90}")

    timestamps = [(off, d) for off, v1, v2, d, i in changed_u32 if "TIMESTAMP" in i]
    frames = [(off, d) for off, v1, v2, d, i in changed_u32 if "FRAMES" in i]

    if timestamps:
        print("Timestamp candidates:")
        for off, d in timestamps:
            print(f"  offset {off}: Δ{d} ms")
    if frames:
        print("Frame counter candidates:")
        for off, d in frames:
            print(f"  offset {off}: Δ{d} in {poll_delay:.1f}s ≈ {d/poll_delay:.0f} FPS")
    if float_candidates:
        fps_floats = [(off, f1, f2) for off, f1, f2 in float_candidates
                      if abs(f1 - f2) > 0.01]
        if fps_floats:
            print("Float FPS candidates (values that changed):")
            for off, f1, f2 in fps_floats:
                print(f"  offset {off}: {f1:.2f} → {f2:.2f}")

    if frames:
        best = frames[0]
        print(f"\nOFFSET_FRAMES  = {best[0]}")
    if timestamps:
        # Pick the second timestamp (likely dwTime1)
        if len(timestamps) >= 2:
            print(f"OFFSET_TIME0   = {timestamps[0][0]}")
            print(f"OFFSET_TIME1   = {timestamps[1][0]}")
        else:
            print(f"OFFSET_TIME1   = {timestamps[0][0]}")


def continuous_monitor(entry_base, entry_size, offsets_to_watch, interval=0.5):
    """
    Continuously poll specific offsets and display values.
    Useful for comparing with RTSS OSD in real time.

    offsets_to_watch: list of (offset, label, type) where type is 'u32' or 'f32'
    """
    print(f"\n{'='*80}")
    print("CONTINUOUS MONITOR (Ctrl+C to stop)")
    print(f"{'='*80}")

    header = "  ".join(f"{label:>12s}" for _, label, _ in offsets_to_watch)
    print(f"{'Time':>7s}  {header}  {'DeltaFPS':>10s}")
    print("-" * (10 + 14 * len(offsets_to_watch) + 12))

    prev_snap = None
    t0 = time.monotonic()

    try:
        while True:
            raw = snapshot_entry(entry_base, entry_size)
            elapsed = time.monotonic() - t0

            values = []
            for off, label, typ in offsets_to_watch:
                if typ == 'f32':
                    v = struct.unpack_from("<f", raw, off)[0]
                    values.append((off, v, f"{v:>12.2f}"))
                else:
                    v = struct.unpack_from("<I", raw, off)[0]
                    values.append((off, v, f"{v:>12d}"))

            # Compute delta FPS from frame counter candidates
            delta_fps = ""
            if prev_snap is not None:
                for off, label, typ in offsets_to_watch:
                    if "frame" in label.lower() and typ == 'u32':
                        v_now = struct.unpack_from("<I", raw, off)[0]
                        v_prev = struct.unpack_from("<I", prev_snap[0], off)[0]
                        dt = time.monotonic() - prev_snap[1]
                        if dt > 0 and v_now >= v_prev:
                            fps = (v_now - v_prev) / dt
                            delta_fps = f"{fps:>10.1f}"

            vals_str = "  ".join(s for _, _, s in values)
            print(f"{elapsed:>6.1f}s  {vals_str}  {delta_fps}")

            prev_snap = (raw, time.monotonic())
            time.sleep(interval)

    except KeyboardInterrupt:
        print("\nStopped.")


def main():
    parser = argparse.ArgumentParser(description="RTSS Shared Memory Diagnostic v2")
    parser.add_argument("--poll-delay", type=float, default=1.0,
                        help="Seconds between the two snapshots (default: 1.0)")
    parser.add_argument("--continuous", action="store_true",
                        help="After scan, enter continuous monitor mode")
    parser.add_argument("--watch", type=str, default=None,
                        help="Comma-separated offset:label:type to watch, "
                             "e.g. '272:time1:u32,332:frames:u32,8800:fps:f32'")
    args = parser.parse_args()

    h, base = open_shm()

    sig = read_bytes(base, 0, 4)
    version = read_u32(base, 4)
    entry_size = read_u32(base, 8)
    arr_offset = read_u32(base, 12)
    arr_size = read_u32(base, 16)

    print(f"Signature:      {sig}")
    print(f"Version:        0x{version:08X}")
    print(f"App entry size: {entry_size} bytes")
    print(f"App arr offset: {arr_offset}")
    print(f"App arr count:  {arr_size}")

    # Find active entry
    active_entry_base = None
    for i in range(arr_size):
        eb = base + arr_offset + i * entry_size
        pid = read_u32(eb, 0)
        if pid != 0:
            active_entry_base = eb
            break

    if active_entry_base is None:
        print("\nNo active app entries. Is a game running and hooked by RTSS?")
        kernel32.UnmapViewOfFile(base)
        kernel32.CloseHandle(h)
        return

    scan_and_compare(active_entry_base, entry_size, args.poll_delay)

    if args.continuous:
        # Parse watch list or use defaults
        if args.watch:
            offsets = []
            for part in args.watch.split(","):
                tokens = part.strip().split(":")
                off = int(tokens[0])
                label = tokens[1] if len(tokens) > 1 else f"@{off}"
                typ = tokens[2] if len(tokens) > 2 else "u32"
                offsets.append((off, label, typ))
        else:
            # Default: watch the candidates printed above
            print("\nNo --watch specified, watching common offsets.")
            print("Re-run with --watch to specify, e.g.:")
            print("  --watch '272:time1:u32,332:frames_lo:u32,8800:stat_frames:u32'")
            offsets = [
                (268, "time0", "u32"),
                (272, "time1", "u32"),
                (332, "frames@332", "u32"),
            ]

        continuous_monitor(active_entry_base, entry_size, offsets, interval=0.5)

    kernel32.UnmapViewOfFile(base)
    kernel32.CloseHandle(h)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("Windows only.")
        sys.exit(1)
    main()
