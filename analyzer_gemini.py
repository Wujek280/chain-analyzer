import argparse
import math
import os
import signal
import struct
import subprocess
import sys
from glob import glob

# --- CONFIGURATION & ENV VARIABLES ---
DEFAULT_DIRS = [
    os.path.expanduser("~/storage/dcim/Camera"),
    os.path.expanduser("~/storage/shared/DCIM/Camera"),
    os.path.expanduser("~/storage/shared/Camera"),
]
ENV_SCAN_DIR = os.environ.get("CHAIN_SCAN_DIR", "").strip()
THRESHOLD_HIGH = float(os.environ.get("CHAIN_THRESH_HIGH", "0.08"))
CHUNK_MS = int(os.environ.get("CHAIN_CHUNK_MS", "10"))
DEFAULT_LIMIT = int(os.environ.get("CHAIN_LIST_LIMIT", "6"))


def resolve_default_directory():
    for path in DEFAULT_DIRS:
        if os.path.isdir(path):
            return path
    return DEFAULT_DIRS[0]


def get_videos(directory):
    """Return all MP4 videos, newest first."""
    video_files = glob(os.path.join(directory, "*.mp4")) + glob(os.path.join(directory, "*.MP4"))
    return sorted(video_files, key=os.path.getmtime, reverse=True)


def get_recent_videos(directory, limit):
    """Return the newest MP4 videos capped to a small menu."""
    if limit < 1:
        return []
    return get_videos(directory)[:limit]


def run_analysis(video_path, chunk_ms, thresh_high):
    """Run one pass of the audio analysis."""
    sample_rate = 16000
    bytes_per_sample = 2
    samples_per_chunk = int(sample_rate * (chunk_ms / 1000.0))
    chunk_bytes = samples_per_chunk * bytes_per_sample

    command = [
        'ffmpeg', '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le',
        '-ar', str(sample_rate), '-ac', '1',
        '-f', 's16le', '-'
    ]
    
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    current_time_ms = 0
    tick_times = []
    last_rms = 0.0

    try:
        while True:
            raw_data = process.stdout.read(chunk_bytes)
            if not raw_data or len(raw_data) < chunk_bytes:
                break

            count = len(raw_data) // bytes_per_sample
            samples = struct.unpack(f"<{count}h", raw_data)
            rms = math.sqrt(sum((s / 32768.0) ** 2 for s in samples) / count) if count > 0 else 0.0

            # Detect a short spike in loudness.
            if rms > thresh_high and (rms - last_rms) > (thresh_high * 0.5):
                # Avoid counting one click multiple times in adjacent chunks.
                if not tick_times or (current_time_ms - tick_times[-1]) > 30:
                    tick_times.append(current_time_ms)

            last_rms = rms
            current_time_ms += chunk_ms
    finally:
        try:
            if process.stdout:
                process.stdout.close()
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()

    intervals = [tick_times[i] - tick_times[i - 1] for i in range(1, len(tick_times))]

    coasting_ticks = 0
    coasting_duration_ms = 0

    for i in range(1, len(intervals)):
        # If the gap grows, the wheel is slowing down.
        if intervals[i] > intervals[i - 1] and intervals[i] < 1000:
            coasting_ticks += 1
            coasting_duration_ms += intervals[i]

    return coasting_ticks, coasting_duration_ms / 1000.0


def analyze_audio_with_fallback(video_path, chunk_ms, thresh_high):
    """Try the analysis with progressively lower thresholds."""
    print(f"\n[1/2] Analyzing: {os.path.basename(video_path)}")

    attempts = [thresh_high, thresh_high * 0.5, thresh_high * 0.25]

    for idx, current_thresh in enumerate(attempts):
        if idx > 0:
            print(f" -> No clear slowdown detected. Retry {idx + 1} with threshold {current_thresh:.4f}")

        ticks, duration = run_analysis(video_path, chunk_ms, current_thresh)

        if ticks > 2:
            print("[2/2] Success: slowdown phase detected.")
            print("-" * 40)
            print(f"Slowdown ticks: {ticks}")
            print(f"Slowdown duration: {duration:.2f} s")
            friction = round((ticks / duration) if duration > 0 else float("inf"), 4)
            print(f"Estimated friction score: {friction}")
            print("-" * 40)
            return

    print("[-] Could not detect a clear slowdown phase, even at higher sensitivity.")


def main():
    parser = argparse.ArgumentParser(description="Chain audio analyzer for Termux")
    parser.add_argument("--dir", default=ENV_SCAN_DIR or resolve_default_directory(), help="Folder with videos")
    parser.add_argument("--high", type=float, default=THRESHOLD_HIGH, help="Click detection threshold")
    parser.add_argument("--chunk", type=int, default=CHUNK_MS, help="Window size in ms")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="How many newest videos to show")
    args = parser.parse_args()

    print(f"Scanning {args.dir}...")
    videos = get_recent_videos(args.dir, args.limit)

    if not videos:
        print(f"No videos found in: {args.dir}")
        sys.exit(0)

    print(f"\nNewest {len(videos)} videos:")
    for idx, v in enumerate(videos, start=1):
        print(f"[{idx}] {os.path.basename(v)}")

    try:
        selection = int(input(f"\nChoose a video number (1-{len(videos)}): "))
        selected_video = videos[selection - 1]
    except (ValueError, IndexError):
        print("Invalid selection.")
        sys.exit(1)

    analyze_audio_with_fallback(selected_video, args.chunk, args.high)

if __name__ == "__main__":
    main()
