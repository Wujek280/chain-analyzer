#! /usr/bin/env python

import argparse
import csv
import math
import os
import signal
import struct
import subprocess
import sys
import textwrap
import wave
from glob import glob
from datetime import datetime

# --- CONFIGURATION & ENV VARIABLES ---
DEFAULT_DIRS = [
    os.path.expanduser("~/storage/dcim/Camera"),
    os.path.expanduser("~/storage/shared/DCIM/Camera"),
    os.path.expanduser("~/storage/shared/Camera"),
]
ENV_SCAN_DIR = os.environ.get("CHAIN_SCAN_DIR", "").strip()
ADAPTIVE_RATIO = float(os.environ.get("CHAIN_ADAPTIVE_RATIO", "3.5"))
CHUNK_MS = int(os.environ.get("CHAIN_CHUNK_MS", "2"))
DEFAULT_LIMIT = int(os.environ.get("CHAIN_LIST_LIMIT", "6"))
DEFAULT_BAND_CENTER = int(os.environ.get("CHAIN_BAND_CENTER", "4000"))
DEFAULT_BAND_RANGE = int(os.environ.get("CHAIN_BAND_RANGE", "500"))
MIN_PEAK_DISTANCE_MS = int(os.environ.get("CHAIN_MIN_PEAK_DISTANCE_MS", "15"))
NOISE_WINDOW_MS = int(os.environ.get("CHAIN_NOISE_WINDOW_MS", "200"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_AUDIO_PATH = os.path.join(SCRIPT_DIR, "last.wav")
LAST_SYNTHETIC_PATH = os.path.join(SCRIPT_DIR, "last-synthetic.wav")
CSV_PATH = os.path.join(SCRIPT_DIR, "results.csv")


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
    if limit < 1:
        return []
    return get_videos(directory)[:limit]


def build_audio_filters(normalize=False, band_center=DEFAULT_BAND_CENTER, band_range=DEFAULT_BAND_RANGE):
    band_low = max(20, band_center - band_range)
    band_high = max(band_low + 1, band_center + band_range)
    filters = [
        "pan=mono|c0=0.5*FL+0.5*FR",
        f"highpass=f={band_low}",
        f"lowpass=f={band_high}",
    ]
    if normalize:
        filters += ["dynaudnorm=gausssize=7", "alimiter=limit=0.98"]
    return filters


# ---------------------------------------------------------------------------
#  Envelope extraction
# ---------------------------------------------------------------------------

def compute_envelope(video_path, chunk_ms, sample_rate=16000, normalize=False,
                     band_center=DEFAULT_BAND_CENTER, band_range=DEFAULT_BAND_RANGE,
                     start_offset_ms=0):
    """Extract audio via ffmpeg and return an RMS envelope (one value per chunk)."""
    bytes_per_sample = 2
    samples_per_chunk = max(1, int(sample_rate * (chunk_ms / 1000.0)))
    chunk_bytes = samples_per_chunk * bytes_per_sample
    skip_bytes = max(0, int(sample_rate * (start_offset_ms / 1000.0))) * bytes_per_sample

    audio_filters = build_audio_filters(normalize=normalize, band_center=band_center, band_range=band_range)
    command = [
        "ffmpeg", "-i", video_path, "-vn",
        "-af", ",".join(audio_filters),
        "-acodec", "pcm_s16le",
        "-ar", str(sample_rate), "-ac", "1",
        "-f", "s16le", "-",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    envelope = []

    try:
        remaining_skip = skip_bytes
        while remaining_skip > 0:
            to_read = min(chunk_bytes, remaining_skip)
            skipped = process.stdout.read(to_read)
            if not skipped:
                break
            remaining_skip -= len(skipped)

        while True:
            raw_data = process.stdout.read(chunk_bytes)
            if not raw_data or len(raw_data) < chunk_bytes:
                break
            count = len(raw_data) // bytes_per_sample
            samples = struct.unpack(f"<{count}h", raw_data)
            rms = math.sqrt(sum((s / 32768.0) ** 2 for s in samples) / count) if count > 0 else 0.0
            envelope.append(rms)
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

    return envelope


# ---------------------------------------------------------------------------
#  Reverse-scan peak detection
# ---------------------------------------------------------------------------

def smooth_series(values, radius=1):
    """Moving average smoothing."""
    if radius <= 0 or len(values) < 3:
        return values[:]
    smoothed = []
    for i in range(len(values)):
        start = max(0, i - radius)
        end = min(len(values), i + radius + 1)
        smoothed.append(sum(values[start:end]) / (end - start))
    return smoothed


def compute_noise_floor(envelope, window_chunks, percentile=0.25):
    """Local noise floor via sliding-window percentile."""
    noise_floor = []
    for i in range(len(envelope)):
        start = max(0, i - window_chunks)
        end = min(len(envelope), i + window_chunks + 1)
        window = sorted(envelope[start:end])
        idx = max(0, min(len(window) - 1, int(len(window) * percentile)))
        noise_floor.append(window[idx])
    return noise_floor


def detect_peaks_reverse(envelope, ratio=3.0, min_distance=2, noise_window=40, min_rms=0.0005):
    """Scan the envelope from end to start, collecting peaks that stand above
    local noise.  Walking backward from the quiet tail ensures we capture the
    deceleration phase first and stop naturally when the signal becomes too
    dense / noisy in the fast-spinning middle section.

    Returns peak indices in chronological (ascending) order.
    """
    if len(envelope) < 3:
        return []

    smoothed = smooth_series(envelope, radius=1)
    noise_floor = compute_noise_floor(smoothed, noise_window)

    peaks = []
    last_peak_idx = len(smoothed) + min_distance  # first pass: no constraint

    for i in range(len(smoothed) - 2, 0, -1):
        if last_peak_idx - i < min_distance:
            continue

        local_thresh = max(noise_floor[i] * ratio, min_rms)
        if smoothed[i] <= local_thresh:
            continue

        # Local maximum (check both neighbours)
        if smoothed[i] > smoothed[i - 1] and smoothed[i] >= smoothed[i + 1]:
            peaks.append(i)
            last_peak_idx = i

    peaks.reverse()
    return peaks


def parabolic_interpolate(envelope, idx):
    """Refine a peak index to sub-chunk precision using parabolic interpolation.

    Fits a parabola through three points (y0, y1, y2) at indices (idx-1, idx, idx+1).
    The vertex offset from center is:

        delta = (y0 - y2) / (2 * (y0 - 2*y1 + y2))

    Refined position = idx + delta
    """
    if idx <= 0 or idx >= len(envelope) - 1:
        return float(idx)
    y0 = envelope[idx - 1]
    y1 = envelope[idx]
    y2 = envelope[idx + 1]
    denom = y0 - 2 * y1 + y2
    if abs(denom) < 1e-12:
        return float(idx)
    delta = (y0 - y2) / (2 * denom)
    return idx + max(-0.5, min(0.5, delta))


def extract_peak_times_ms(envelope, chunk_ms, ratio, min_distance_ms, noise_window_ms):
    """Convert envelope into peak timestamps using reverse-scan detection
    with parabolic interpolation for sub-chunk precision."""
    min_distance_chunks = max(2, int(min_distance_ms / max(chunk_ms, 1)))
    noise_window_chunks = max(1, int(noise_window_ms / max(chunk_ms, 1)))
    peak_indices = detect_peaks_reverse(
        envelope, ratio=ratio, min_distance=min_distance_chunks,
        noise_window=noise_window_chunks,
    )
    smoothed = smooth_series(envelope, radius=1)
    return [parabolic_interpolate(smoothed, idx) * chunk_ms for idx in peak_indices]


# ---------------------------------------------------------------------------
#  Phase refinement
# ---------------------------------------------------------------------------

def refine_peak_times_by_phase(peak_time_runs):
    """Average peak timestamps across multiple phase-shifted runs."""
    if not peak_time_runs:
        return []
    common_length = min(len(run) for run in peak_time_runs)
    if common_length <= 0:
        return []
    refined = []
    for idx in range(common_length):
        samples = [run[idx] for run in peak_time_runs if idx < len(run)]
        refined.append(round(sum(samples) / len(samples), 2))
    return refined


# ---------------------------------------------------------------------------
#  Interval analysis & decline coefficient
# ---------------------------------------------------------------------------

def render_interval_bars(intervals_ms, width=20):
    if not intervals_ms:
        return []
    min_interval = min(intervals_ms)
    max_interval = max(intervals_ms)
    if max_interval <= min_interval:
        return ["[]" for _ in intervals_ms]
    bars = []
    for interval in intervals_ms:
        normalized = (interval - min_interval) / (max_interval - min_interval)
        count = max(1, int(round(normalized * width)))
        bars.append("[]" * count)
    return bars


def extract_deceleration(intervals_ms, cutoff_ms=35):
    """Walk backward from the last interval and include everything >= cutoff_ms.
    Stop at the first interval below the cutoff.

    Returns (start_index, decel_intervals).
    """
    if len(intervals_ms) < 2:
        return 0, intervals_ms[:]

    start = len(intervals_ms)
    for i in range(len(intervals_ms) - 1, -1, -1):
        if intervals_ms[i] < cutoff_ms:
            break
        start = i

    if start >= len(intervals_ms):
        start = len(intervals_ms) - 1

    return start, intervals_ms[start:]


def compute_coefficient(intervals_ms):
    """Average per-step slowdown rate and standard deviation."""
    if len(intervals_ms) < 2:
        return None, None
    rates = []
    for prev, current in zip(intervals_ms, intervals_ms[1:]):
        if prev > 0:
            rates.append((current - prev) / prev)
    if not rates:
        return None, None
    mean = sum(rates) / len(rates)
    variance = sum((r - mean) ** 2 for r in rates) / len(rates)
    return mean, math.sqrt(variance)


def compute_log_coefficient(intervals_ms):
    """Linear regression of log(interval) on step index.

    Friction in the deceleration tail makes intervals grow roughly like
    interval(n) = A * exp(k*n).  Taking log() linearises that, so the slope
    of log(interval) vs. step index is a stable estimate of the exponential
    growth rate k.  Returns (k, r_squared); k > 0 means slowing down.
    """
    if len(intervals_ms) < 2:
        return None, None
    xs, ys = [], []
    for i, interval in enumerate(intervals_ms):
        if interval > 0:
            xs.append(float(i))
            ys.append(math.log(interval))
    if len(ys) < 2:
        return None, None
    n = len(ys)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None, None
    slope = num / den
    intercept = mean_y - slope * mean_x
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return slope, r_squared


# ---------------------------------------------------------------------------
#  Single-pass analysis
# ---------------------------------------------------------------------------

def run_analysis(video_path, chunk_ms, ratio, normalize=False,
                 band_center=DEFAULT_BAND_CENTER, band_range=DEFAULT_BAND_RANGE,
                 min_peak_distance_ms=MIN_PEAK_DISTANCE_MS,
                 noise_window_ms=NOISE_WINDOW_MS, start_offset_ms=0):
    """Run one pass: extract envelope -> reverse-scan peak detection."""
    envelope = compute_envelope(
        video_path, chunk_ms, normalize=normalize,
        band_center=band_center, band_range=band_range,
        start_offset_ms=start_offset_ms,
    )
    peak_times_ms = extract_peak_times_ms(
        envelope, chunk_ms, ratio, min_peak_distance_ms, noise_window_ms,
    )
    if start_offset_ms:
        peak_times_ms = [t + start_offset_ms for t in peak_times_ms]
    return peak_times_ms


# ---------------------------------------------------------------------------
#  Audio helpers (debug / export)
# ---------------------------------------------------------------------------

def dump_processed_audio(video_path, normalize=False, band_center=DEFAULT_BAND_CENTER, band_range=DEFAULT_BAND_RANGE):
    """Write the exact filtered audio to last.wav for inspection."""
    audio_filters = build_audio_filters(normalize=normalize, band_center=band_center, band_range=band_range)
    command = [
        "ffmpeg", "-y", "-i", video_path, "-vn",
        "-af", ",".join(audio_filters),
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        LAST_AUDIO_PATH,
    ]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def get_wav_duration_ms(path):
    with wave.open(path, "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
        if rate <= 0:
            return 0
        return int((frames / float(rate)) * 1000)


def build_sine_click(sample_rate, duration_ms, frequency_hz=4000, amplitude=0.85):
    sample_count = max(1, int(sample_rate * (duration_ms / 1000.0)))
    samples = []
    for i in range(sample_count):
        value = amplitude * math.sin(2.0 * math.pi * frequency_hz * (i / sample_rate))
        samples.append(int(max(-1.0, min(1.0, value)) * 32767))
    return samples


def dump_synthetic_audio(peak_times_ms, duration_ms):
    """Write a debug WAV with short 4 kHz clicks at detected peak timestamps."""
    sample_rate = 16000
    click_duration_ms = 4
    click_samples = build_sine_click(sample_rate, click_duration_ms)
    click_offset_ms = click_duration_ms / 2.0
    total_samples = max(1, int(sample_rate * (duration_ms / 1000.0)))
    audio = [0] * total_samples

    for peak_ms in peak_times_ms:
        start_idx = int(sample_rate * ((peak_ms - click_offset_ms) / 1000.0))
        for offset, sample in enumerate(click_samples):
            idx = start_idx + offset
            if idx >= total_samples:
                break
            if idx < 0:
                continue
            mixed = audio[idx] + sample
            audio[idx] = max(-32768, min(32767, mixed))

    with wave.open(LAST_SYNTHETIC_PATH, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        pcm = struct.pack(f"<{len(audio)}h", *audio)
        wav_file.writeframes(pcm)

    print(f" -> Dumped synthetic debug audio to: {LAST_SYNTHETIC_PATH}")


def save_result_row(k_value):
    """Append a timestamp, description, and final k-value to the CSV log."""
    description = input("Enter short description for this run: ").strip()
    timestamp = datetime.now().isoformat(timespec="seconds")
    row = [
        timestamp,
        description,
        "" if k_value is None else f"{k_value:.2f}",
    ]

    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not file_exists:
            writer.writerow(["timestamp", "description", "k_value"])
        writer.writerow(row)

    print(f" -> Saved CSV row to: {CSV_PATH}")


# ---------------------------------------------------------------------------
#  Main analysis orchestrator
# ---------------------------------------------------------------------------

def analyze_video(video_path, chunk_ms, ratio, normalize=False,
                  band_center=DEFAULT_BAND_CENTER, band_range=DEFAULT_BAND_RANGE,
                  min_peak_distance_ms=MIN_PEAK_DISTANCE_MS,
                  noise_window_ms=NOISE_WINDOW_MS,
                  cutoff_ms=35, shift_ms=None, save=False, debug=False):
    """Reverse-scan peak detection: find clicks from the tail backward, extract
    the deceleration phase, and compute the slowdown coefficient."""
    print(f"\n[1/2] Analyzing: {os.path.basename(video_path)}")
    print(f" -> Band-pass: {band_center - band_range} Hz to {band_center + band_range} Hz")
    print(f" -> Adaptive ratio: {ratio}")
    if normalize:
        print(" -> Audio normalization: on")

    try:
        dump_processed_audio(video_path, normalize=normalize,
                             band_center=band_center, band_range=band_range)
        print(f" -> Dumped processed audio to: {LAST_AUDIO_PATH}")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f" -> Warning: could not dump processed audio ({exc})")

    if shift_ms is not None and shift_ms > 0:
        # Phase-shift averaging: run at offsets 0, shift, 2*shift, ...
        offsets = list(range(0, chunk_ms, shift_ms))
        if not offsets:
            offsets = [0]
        print(f" -> Phase-shift averaging: offsets {offsets} ms")
        phase_runs = []
        for offset in offsets:
            peaks = run_analysis(
                video_path, chunk_ms, ratio, normalize=normalize,
                band_center=band_center, band_range=band_range,
                min_peak_distance_ms=min_peak_distance_ms,
                noise_window_ms=noise_window_ms,
                start_offset_ms=offset,
            )
            phase_runs.append(peaks)
        peak_times_ms = refine_peak_times_by_phase(phase_runs)
    else:
        # Single pass (default)
        peak_times_ms = run_analysis(
            video_path, chunk_ms, ratio, normalize=normalize,
            band_center=band_center, band_range=band_range,
            min_peak_distance_ms=min_peak_distance_ms,
            noise_window_ms=noise_window_ms,
        )

    if len(peak_times_ms) < 3:
        print("[-] Not enough peaks detected to analyze.")
        return

    all_intervals = [peak_times_ms[i] - peak_times_ms[i - 1] for i in range(1, len(peak_times_ms))]

    # Extract the deceleration tail (walking backward from end)
    decel_start, decel_intervals = extract_deceleration(all_intervals, cutoff_ms=cutoff_ms)
    decel_peaks = peak_times_ms[decel_start:]

    print("[2/2] Results")
    print("=" * 50)
    print(f"Total peaks detected: {len(peak_times_ms)}")
    print(f"Deceleration phase: {len(decel_peaks)} peaks "
          f"({int(round(decel_peaks[0]))} ms to {int(round(decel_peaks[-1]))} ms)")
    print(f"Peak times (ms): {[int(round(x)) for x in decel_peaks]}")

    print(f"\n--- Deceleration intervals ({len(decel_intervals)}) ---")
    for interval, bar in zip(decel_intervals, render_interval_bars(decel_intervals)):
        print(f"  {int(round(interval)):4d} ms {bar}")

    # Last 4 intervals — consistent metric regardless of total peak count
    # Shifted by -1 to drop the unstable final click: uses n-4..n-1 from the end.
    if len(decel_intervals) >= 5:
        last4 = decel_intervals[-5:-1]
    elif len(decel_intervals) >= 2:
        last4 = decel_intervals[:-1]
    else:
        last4 = decel_intervals
    coeff4, _stdev4 = compute_coefficient(last4)
    log_coeff4, log_r2 = compute_log_coefficient(last4)

    if log_coeff4 is not None:
        print(f"\nK-value (last): {log_coeff4:.2f}")
    if coeff4 is None and log_coeff4 is None:
        print("\n[-] Could not compute deceleration coefficient.")
    print("=" * 50)

    if save and log_coeff4 is not None:
        save_result_row(log_coeff4)

    if debug:
        try:
            duration_ms = get_wav_duration_ms(LAST_AUDIO_PATH)
            dump_synthetic_audio(decel_peaks, duration_ms)
        except (OSError, wave.Error, struct.error) as exc:
            print(f" -> Warning: could not dump synthetic debug audio ({exc})")


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def build_parser():
    description = "Chain audio analyzer for Termux"
    epilog = textwrap.dedent(
        """
        Examples:
          python analyzer_gemini.py
          python analyzer_gemini.py --normalize
          python analyzer_gemini.py --dir ~/Videos --limit 6
          python analyzer_gemini.py --high 2.0  (more sensitive)
        """
    ).strip()
    parser = argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dir",
        default=ENV_SCAN_DIR or resolve_default_directory(),
        help="Folder with videos to scan. Default: Termux camera folder or CHAIN_SCAN_DIR.",
    )
    parser.add_argument(
        "--high",
        type=float,
        default=ADAPTIVE_RATIO,
        help="Adaptive peak ratio: a peak must exceed local noise floor by this factor. "
             "Lower = more sensitive. Default: 3.5.",
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=CHUNK_MS,
        help="Envelope window size in milliseconds. Default: 2.",
    )
    parser.add_argument(
        "--min-peak-distance",
        type=int,
        default=MIN_PEAK_DISTANCE_MS,
        help="Minimum spacing between detected peaks in milliseconds. Default: 15.",
    )
    parser.add_argument(
        "--noise-window",
        type=int,
        default=NOISE_WINDOW_MS,
        help="Sliding window size in ms for local noise floor estimation. Default: 200.",
    )
    parser.add_argument(
        "--from",
        dest="cutoff_ms",
        type=int,
        default=35,
        help="Minimum interval in ms to include in deceleration phase. "
             "Walks backward from the last peak and stops at the first interval below this. Default: 35.",
    )
    parser.add_argument(
        "--shift",
        type=int,
        default=None,
        help="Enable phase-shift averaging with this step size in ms. "
             "Runs detection at offsets 0, shift, 2*shift, ... up to chunk size, "
             "then averages timestamps. Off by default.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="How many newest videos to show in the menu.",
    )
    parser.add_argument(
        "--select",
        type=int,
        default=None,
        help="Auto-select a menu item by number and skip the prompt. Uses 1-based numbering.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Append the timestamp, description, and coefficient to results.csv next to the script.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Write last-synthetic.wav with 4 kHz debug clicks at detected timestamps.",
    )
    parser.add_argument(
        "--normalize",
        dest="normalize",
        action="store_true",
        default=False,
        help="Apply dynamic normalization before peak detection.",
    )
    parser.add_argument(
        "--no-normalize",
        dest="normalize",
        action="store_false",
        help="Disable dynamic normalization (default).",
    )
    parser.add_argument(
        "--band",
        type=int,
        default=DEFAULT_BAND_CENTER,
        help="Center frequency for the band-pass filter in Hz. Default: 4000.",
    )
    parser.add_argument(
        "--range",
        dest="band_range",
        type=int,
        default=DEFAULT_BAND_RANGE,
        help="Half-width of the band-pass filter in Hz. Default: 500.",
    )
    parser.add_argument(
        "--band-range",
        dest="band_range",
        type=int,
        help=argparse.SUPPRESS,
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    print(f"Scanning {args.dir}...")
    videos = get_recent_videos(args.dir, args.limit)

    if not videos:
        print(f"No videos found in: {args.dir}")
        sys.exit(0)

    print(f"\nNewest {len(videos)} videos:")
    for idx, v in enumerate(videos, start=1):
        print(f"[{idx}] {os.path.basename(v)}")

    if args.select is not None:
        selection = args.select
        if selection < 1 or selection > len(videos):
            print(f"Invalid selection: {selection}. Choose 1-{len(videos)}.")
            sys.exit(1)
        selected_video = videos[selection - 1]
        print(f"\nAuto-selected video {selection}: {os.path.basename(selected_video)}")
    else:
        try:
            selection = int(input(f"\nChoose a video number (1-{len(videos)}): "))
            selected_video = videos[selection - 1]
        except (ValueError, IndexError):
            print("Invalid selection.")
            sys.exit(1)

    analyze_video(
        selected_video,
        args.chunk,
        args.high,
        normalize=args.normalize,
        band_center=args.band,
        band_range=args.band_range,
        min_peak_distance_ms=args.min_peak_distance,
        noise_window_ms=args.noise_window,
        cutoff_ms=args.cutoff_ms,
        shift_ms=args.shift,
        save=args.save,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
