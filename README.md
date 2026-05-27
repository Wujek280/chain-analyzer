# Chain Analyzer (Termux / Mobile Friendly)

Analyze chain spin-down videos on Android (via Termux) using `ffmpeg` and Python.

## What this does

- Scans a video folder for chain videos
- Extracts audio with `ffmpeg`
- Detects peak events from the audio envelope
- Writes results to `results.csv`

## Quick setup on Termux (recommended)

1. Install [Termux](https://termux.dev/) on your Android device.
2. Open Termux and grant storage access:

```bash
termux-setup-storage
```

3. Clone or copy this project into Termux.
4. From the project directory, run:

```bash
bash install_termux.sh
```

This installs `python`, `ffmpeg`, and Python dependencies.

## Manual setup on Termux

If you prefer manual install:

```bash
git clone https://github.com/<your-username>/chain-analyzer.git
cd chain-analyzer
pkg update -y
pkg install -y python ffmpeg
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Usage

Run from the project directory:

```bash
python chain_analyser.py
```

Common options:

```bash
python chain_analyser.py --limit 6
python chain_analyser.py --dir ~/storage/shared/DCIM/Camera
```

## Example Output

```sh
╭─u0_a387@localhost ~/storage/documents/chain-analyzer ‹main›
╰─$ python chain_analyser.py --select 3
Scanning /data/data/com.termux/files/home/storage/dcim/Camera...

Newest 6 videos:
[1] PXL_20260527_161036667.mp4
[2] PXL_20260527_155143765.mp4
[3] PXL_20260526_175003400~2.mp4
[4] PXL_20260526_175003400.mp4
[5] PXL_20260526_173659308.mp4
[6] PXL_20260503_180603971.mp4

Auto-selected video 3: PXL_20260526_175003400~2.mp4

[1/2] Analyzing: PXL_20260526_175003400~2.mp4
 -> Band-pass: 3500 Hz to 4500 Hz
 -> Adaptive ratio: 3.5
 -> Dumped processed audio to: /storage/emulated/0/Documents/chain-analyzer/last.wav
[2/2] Results
==================================================
Total peaks detected: 40
1.5 - 2.2s (14 peaks detected)

--- Deceleration intervals (13) ---
  n12   35 ms  []
  n11   37 ms  []
  n10   39 ms  []
  n9    38 ms  []
  n8    42 ms  [][]
  n7    44 ms  [][]
  n6    48 ms  [][][]
  n5    50 ms  [][][][]
  n4    54 ms  [][][][][]
  n3    61 ms  [][][][][][][]
  n2    67 ms  [][][][][][][][][]
  n1    82 ms  [][][][][][][][][][][][][]
  n0   108 ms  [][][][][][][][][][][][][][][][][][][][]

K-value (last n1..n4): 0.13 (R²=0.975)
==================================================

```


## Environment variables:

```bash
export CHAIN_SCAN_DIR=~/storage/shared/DCIM/Camera
export CHAIN_NORMALIZE=1
python chain_analyser.py
```

## Notes for mobile paths

After `termux-setup-storage`, shared storage is typically available under:

- `~/storage/shared`
- Camera videos often at `~/storage/shared/DCIM/Camera`

If your files are elsewhere, pass the exact folder with `--dir`.
