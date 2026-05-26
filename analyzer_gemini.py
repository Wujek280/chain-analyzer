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
THRESHOLD_HIGH = float(os.environ.get("CHAIN_THRESH_HIGH", "0.08"))
CHUNK_MS = int(os.environ.get("CHAIN_CHUNK_MS", "5"))
DEFAULT_LIMIT = int(os.environ.get("CHAIN_LIST_LIMIT", "6"))
ENV_NORMALIZE = os.environ.get("CHAIN_NORMALIZE", "").strip().lower() in {"false", "no", "off"}
DEFAULT_BAND_CENTER = int(os.environ.get("CHAIN_BAND_CENTER", "4000"))
DEFAULT_BAND_RANGE = int(os.environ.get("CHAIN_BAND_RANGE", "200"))
MIN_PEAK_DISTANCE_MS = int(os.environ.get("CHAIN_MIN_PEAK_DISTANCE_MS", "8"))
PHASE_OFFSETS_MS = tuple(
    sorted(
        {
            max(0, int(part.strip()))
            for part in os.environ.get("CHAIN_PHASE_OFFSETS_MS", "0,2,4").split(",")
            if part.strip()
        }
    )
)
if not PHASE_OFFSETS_MS:
    PHASE_OFFSETS_MS = (0, 2, 4)
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
    """Return the newest MP4 videos capped to a small menu."""
    if limit < 1:
        return []
    return get_videos(directory)[:limit]


def build_audio_filters(normalize=False, band_center=DEFAULT_BAND_CENTER, band_range=DEFAULT_BAND_RANGE):
    """Build the shared ffmpeg filter chain used for export and analysis."""
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


def smooth_series(values, radius=1):
    """Apply a tiny moving average to stabilize the energy envelope."""
    if radius <= 0 or len(values) < 3:
        return values[:]

    smoothed = []
    for i in range(len(values)):
        start = max(0, i - radius)
        end = min(len(values), i + radius + 1)
        smoothed.append(sum(values[start:end]) / (end - start))
    return smoothed


def detect_peaks(values, threshold, min_distance=4):
    """Find local maxima in the energy envelope."""
    if len(values) < 3:
        return []

    peaks = []
    last_peak_idx = -min_distance

    for i in range(1, len(values) - 1):
        if i - last_peak_idx < min_distance:
            continue

        current = values[i]
        if current <= threshold:
            continue

        if current > values[i - 1] and current >= values[i + 1]:
            peaks.append(i)
            last_peak_idx = i

    return peaks


def extract_peak_times_ms(envelope, chunk_ms, threshold, min_distance_ms=MIN_PEAK_DISTANCE_MS):
    """Convert the smoothed envelope into peak timestamps."""
    smoothed = smooth_series(envelope, radius=1)
    min_distance_chunks = max(2, int(min_distance_ms / max(chunk_ms, 1)))
    peak_indices = detect_peaks(smoothed, threshold, min_distance=min_distance_chunks)
    return [idx * chunk_ms for idx in peak_indices]


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


def render_interval_bars(intervals_ms, width=20):
    """Render intervals as a normalized ASCII bar sequence."""
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


def compute_decline_coefficient(peak_times_ms):
    """Compute the average slowdown rate from consecutive peak intervals."""
    intervals_ms = [peak_times_ms[i] - peak_times_ms[i - 1] for i in range(1, len(peak_times_ms))]
    if len(intervals_ms) < 2:
        return {
            "coefficient": None,
            "intervals_ms": intervals_ms,
            "trimmed_intervals_ms": [],
            "rising_pairs": [],
        }

    trimmed = intervals_ms[1:-1] if len(intervals_ms) > 4 else intervals_ms[:]
    rising_pairs = []

    for idx, (prev, current) in enumerate(zip(trimmed, trimmed[1:])):
        if current > prev and prev > 0:
            rising_pairs.append(
                {
                    "index": idx,
                    "prev_ms": prev,
                    "current_ms": current,
                    "slowdown_rate": (current - prev) / prev,
                }
            )

    if not rising_pairs:
        return {
            "coefficient": None,
            "intervals_ms": intervals_ms,
            "trimmed_intervals_ms": trimmed,
            "rising_pairs": rising_pairs,
        }

    slowdown_rates = [item["slowdown_rate"] for item in rising_pairs]

    return {
        "coefficient": sum(slowdown_rates) / len(slowdown_rates),
        "intervals_ms": intervals_ms,
        "trimmed_intervals_ms": trimmed,
        "rising_pairs": rising_pairs,
    }


def run_analysis(
    video_path,
    chunk_ms,
    thresh_high,
    normalize=False,
    band_center=DEFAULT_BAND_CENTER,
    band_range=DEFAULT_BAND_RANGE,
    min_peak_distance_ms=MIN_PEAK_DISTANCE_MS,
    start_offset_ms=0,
):
    """Run one pass of the audio analysis."""
    sample_rate = 16000
    bytes_per_sample = 2
    samples_per_chunk = max(1, int(sample_rate * (chunk_ms / 1000.0)))
    chunk_bytes = samples_per_chunk * bytes_per_sample
    skip_bytes = max(0, int(sample_rate * (start_offset_ms / 1000.0))) * bytes_per_sample

    audio_filters = build_audio_filters(normalize=normalize, band_center=band_center, band_range=band_range)

    command = [
        'ffmpeg', '-i', video_path,
        '-vn',
        '-af', ','.join(audio_filters),
        '-acodec', 'pcm_s16le',
        '-ar', str(sample_rate), '-ac', '1',
        '-f', 's16le', '-'
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

    peak_times_ms = extract_peak_times_ms(envelope, chunk_ms, thresh_high, min_distance_ms=min_peak_distance_ms)
    if start_offset_ms:
        peak_times_ms = [t + start_offset_ms for t in peak_times_ms]
    decline = compute_decline_coefficient(peak_times_ms)

    return {
        "peak_count": len(peak_times_ms),
        "peak_times_ms": peak_times_ms,
        "intervals_ms": decline["intervals_ms"],
        "trimmed_intervals_ms": decline["trimmed_intervals_ms"],
        "rising_pairs": decline["rising_pairs"],
        "decline_coefficient": decline["coefficient"],
    }


def dump_processed_audio(video_path, normalize=False, band_center=DEFAULT_BAND_CENTER, band_range=DEFAULT_BAND_RANGE):
    """Write the exact filtered audio to last.wav for inspection."""
    audio_filters = build_audio_filters(normalize=normalize, band_center=band_center, band_range=band_range)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-vn",
        "-af",
        ",".join(audio_filters),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        LAST_AUDIO_PATH,
    ]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def get_wav_duration_ms(path):
    """Return WAV duration in milliseconds."""
    with wave.open(path, "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
        if rate <= 0:
            return 0
        return int((frames / float(rate)) * 1000)


def build_sine_click(sample_rate, duration_ms, frequency_hz=4000, amplitude=0.85):
    """Generate a short sine click as 16-bit PCM samples."""
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
        start_idx = int(sample_rate * (((peak_ms - click_offset_ms) / 1000.0)))
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


def save_result_row(coefficient):
    """Append a timestamp, description, and coefficient to the CSV log."""
    description = input("Enter short description for this run: ").strip()
    timestamp = datetime.now().isoformat(timespec="seconds")
    row = [timestamp, description, "" if coefficient is None else f"{coefficient:.2f}"]

    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not file_exists:
            writer.writerow(["timestamp", "description", "coefficient"])
        writer.writerow(row)

    print(f" -> Saved CSV row to: {CSV_PATH}")


def analyze_audio_with_fallback(
    video_path,
    chunk_ms,
    thresh_high,
    normalize=False,
    band_center=DEFAULT_BAND_CENTER,
    band_range=DEFAULT_BAND_RANGE,
    min_peak_distance_ms=MIN_PEAK_DISTANCE_MS,
    save=False,
    debug=False,
):
    """Try the analysis with progressively lower thresholds."""
    print(f"\n[1/2] Analyzing: {os.path.basename(video_path)}")
    print(f" -> Band-pass: {band_center - band_range} Hz to {band_center + band_range} Hz")
    if normalize:
        print(" -> Audio normalization: on")

    try:
        dump_processed_audio(
            video_path,
            normalize=normalize,
            band_center=band_center,
            band_range=band_range,
        )
        print(f" -> Dumped processed audio to: {LAST_AUDIO_PATH}")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f" -> Warning: could not dump processed audio to last.wav ({exc})")

    attempts = [thresh_high, thresh_high * 0.25, thresh_high * 0.10]

    for idx, current_thresh in enumerate(attempts):
        if idx > 0:
            print(f" -> No stable peak chain detected. Retry {idx + 1} with threshold {current_thresh:.4f}")

        phase_runs = []
        for phase_offset in PHASE_OFFSETS_MS:
            phase_runs.append(
                run_analysis(
                    video_path,
                    chunk_ms,
                    current_thresh,
                    normalize=normalize,
                    band_center=band_center,
                    band_range=band_range,
                    min_peak_distance_ms=min_peak_distance_ms,
                    start_offset_ms=phase_offset,
                )["peak_times_ms"]
            )

        peak_times_ms = refine_peak_times_by_phase(phase_runs)
        decline = compute_decline_coefficient(peak_times_ms)
        result = {
            "peak_count": len(peak_times_ms),
            "peak_times_ms": peak_times_ms,
            "intervals_ms": decline["intervals_ms"],
            "trimmed_intervals_ms": decline["trimmed_intervals_ms"],
            "rising_pairs": decline["rising_pairs"],
            "decline_coefficient": decline["coefficient"],
        }

        if result["decline_coefficient"] is not None and len(result["trimmed_intervals_ms"]) >= 2:
            print("[2/2] Success: peak decline detected.")
            print("-" * 40)
            print(f"Phase offsets (ms): {list(PHASE_OFFSETS_MS)}")
            print(f"Detected peaks: {result['peak_count']}")
            print(f"Peak times (ms): {[int(round(x)) for x in result['peak_times_ms']]}")
            print(f"Intervals (ms): {[int(round(x)) for x in result['trimmed_intervals_ms']]}")
            print("Interval bars:")
            for interval, bar in zip(result["trimmed_intervals_ms"], render_interval_bars(result["trimmed_intervals_ms"])):
                print(f"  {int(round(interval))} ms {bar}")
            print("Rising interval pairs:")
            for item in result["rising_pairs"]:
                print(
                    f"  pair {item['index'] + 1}: {int(round(item['prev_ms']))} -> {int(round(item['current_ms']))} ms "
                    f"(slowdown {item['slowdown_rate']:.2f})"
                )
            print(f"Average slowdown coefficient: {result['decline_coefficient']:.2f}")
            print("-" * 40)
            if save:
                save_result_row(result["decline_coefficient"])
            if debug:
                try:
                    duration_ms = get_wav_duration_ms(LAST_AUDIO_PATH)
                    dump_synthetic_audio(result["peak_times_ms"], duration_ms)
                except (OSError, wave.Error, struct.error) as exc:
                    print(f" -> Warning: could not dump synthetic debug audio ({exc})")
            return

    print("[-] Could not detect a stable rising-interval pattern, even at higher sensitivity.")


def build_parser():
    description = "Chain audio analyzer for Termux"
    epilog = textwrap.dedent(
        """
        Examples:
          python analyzer_gemini.py
          python analyzer_gemini.py --normalize
          python analyzer_gemini.py --dir ~/Videos --limit 6
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
        default=THRESHOLD_HIGH,
        help="Peak detection threshold used on the filtered audio envelope. Lower = more sensitive.",
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=CHUNK_MS,
        help="Envelope window size in milliseconds. Smaller = more detailed, larger = smoother. Default: 5.",
    )
    parser.add_argument(
        "--min-peak-distance",
        type=int,
        default=MIN_PEAK_DISTANCE_MS,
        help="Minimum spacing between detected peaks in milliseconds. Default: 8.",
    )
    parser.add_argument(
        "--phase-offsets",
        default="0,2,4",
        help="Comma-separated chunk offsets in milliseconds used for timestamp averaging. Default: 0,2,4.",
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
        default=True,
        help="Apply dynamic normalization before peak detection.",
    )
    parser.add_argument(
        "--no-normalize",
        dest="normalize",
        action="store_false",
        help="Disable dynamic normalization for this run.",
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
        help="Half-width of the band-pass filter in Hz. Default: 200.",
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
    global PHASE_OFFSETS_MS
    PHASE_OFFSETS_MS = tuple(
        sorted(
            {
                max(0, int(part.strip()))
                for part in str(args.phase_offsets).split(",")
                if part.strip()
            }
        )
    )
    if not PHASE_OFFSETS_MS:
        PHASE_OFFSETS_MS = (0, 2, 4)

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

    analyze_audio_with_fallback(
        selected_video,
        args.chunk,
        args.high,
        normalize=args.normalize,
        band_center=args.band,
        band_range=args.band_range,
        min_peak_distance_ms=args.min_peak_distance,
        save=args.save,
        debug=args.debug,
    )

if __name__ == "__main__":
    main()
