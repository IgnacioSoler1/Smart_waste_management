# SmartWaste MVD — pipeline results (final)

- Range: 2026-10-02 → 2026-10-06 (5 days, 657 circuit-days, 134 circuits)
- Observed mean fill level: 53.4% (daily range 43.1–57.6%)

| Metric | Mean ± SD | Median | 95% CI (bootstrap) |
|---|---|---|---|
| Distance reduction (%) ↑ | 55.63 ± 25.09 | 60.3 | [53.68, 57.55] |
| Travel-time reduction (%) ↑ | 59.19 ± 22.61 | 62.2 | [57.41, 60.91] |

- Mean static baseline: 115.2 km; mean optimized: 45.6 km
- Containers skipped per circuit-day: 15.4 (16.7%)
- Wilcoxon (baseline km > optimized km): W+=215087, p=1.90e-107, r_rb=0.99, n=657

## By shift

| Shift | Circuits/day | Distance reduction (%) ↑ | Skipped (%) |
|---|---|---|---|
| afternoon | 10.4 | 49.3 | 8.9 |
| morning | 59.2 | 64.1 | 19.3 |
| night | 61.8 | 48.6 | 15.5 |
