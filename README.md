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

You can also use environment variables:

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
