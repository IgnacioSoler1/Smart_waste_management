# SmartWaste MVD — pipeline results (interim)

- Range: 2026-10-02 → 2026-10-04 (3 days, 389 circuit-days, 133 circuits)
- Observed mean fill level: 50.6% (daily range 43.1–57.6%)

| Metric | Mean ± SD | Median | 95% CI (bootstrap) |
|---|---|---|---|
| Distance reduction (%) ↑ | 55.21 ± 25.91 | 60.0 | [52.61, 57.75] |
| Travel-time reduction (%) ↑ | 58.74 ± 23.52 | 62.0 | [56.41, 61.07] |

- Mean static baseline: 115.8 km; mean optimized: 46.2 km
- Containers skipped per circuit-day: 14.9 (16.3%)
- Wilcoxon (baseline km > optimized km): W+=75344, p=4.33e-64, r_rb=0.987, n=389

## By shift

| Shift | Circuits/day | Distance reduction (%) ↑ | Skipped (%) |
|---|---|---|---|
| afternoon | 10 | 52.1 | 9.7 |
| morning | 58 | 64.1 | 19.8 |
| night | 61.7 | 47.4 | 14.2 |
