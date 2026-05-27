# analyzer_gemini.py

`analyzer_gemini.py` is a Termux-friendly chain audio analyzer that processes recorded video/audio and detects lubrication or friction-related chain noise from sound patterns.

It scans a chosen folder of recent videos, extracts audio features, finds acoustic peaks, and estimates a coefficient from the deceleration phase. Optional flags let you tune sensitivity, normalization, filtering, peak spacing, and debug output, and it can save results to `results.csv`.
