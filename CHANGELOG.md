

## LOG UPDATE

  1. Shifted the "last 4" window by -1 (analyzer_gemini.py:500-506)
  Now uses intervals at positions n-4, n-3, n-2, n-1 from the end (dropping the unstable final click) instead of n-3..n as before. Falls back gracefully if fewer than 5
   intervals exist.

  2. New compute_log_coefficient() (analyzer_gemini.py:293-321)
  Linear regression of log(interval_ms) against step index. For an exponential tail interval(n) = A · exp(k·n), the slope is k. Returns (k, R²). No curve fitting
  existed before — only the linear average in compute_coefficient.

  3. New output line in results (analyzer_gemini.py:514-517)
  Prints k, the equivalent per-step growth (exp(k)-1)·100%, and the regression R² so you can see how well the exponential model fits.

  4. CSV save extended (analyzer_gemini.py:404-420)
  Added a log_coefficient column. New rows write 4 cells; the old 3-column header in your existing results.csv won't be overwritten, but newly appended rows will
  include the log value — you may want to manually patch the header to timestamp,description,coefficient,log_coefficient.

  Interpretation tip: if friction follows interval ~ 1/x against speed, you'd expect k > 0 (slowing down). A higher k means harsher friction, and a low R² (well below
  ~0.9) means the exponential assumption itself doesn't hold for that segment.


