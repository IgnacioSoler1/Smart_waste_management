#!/usr/bin/env python3
"""
Aggregates quantitative results from the SmartWaste MVD Gold data lake layer.

Reads all available daily/*.json files from S3 and computes the same metrics
as the paper's Quantitative Results section (Tables 1 and 2), now over the
full available history instead of just 5 days.

Usage:
    python3 docs/results/compute_results.py
    python3 docs/results/compute_results.py --since 2026-09-28 --label interim
    python3 docs/results/compute_results.py --since 2026-09-28 --until 2026-10-12 --label final

Output (sin --label):
    docs/results/quantitative_results.json
Output (con --label X):
    docs/results/quantitative_results_X.json   agregados + IC 95% + Wilcoxon
    docs/results/circuit_days_X.csv            una fila por circuito-día (insumo de analyze_results.py)
    docs/results/summary_X.md                  resumen legible para compartir
"""

import argparse
import csv
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3

BUCKET = "smartwaste-data-lake-dev"
GOLD_DAILY_PREFIX = "analytics-results/daily/"
AWS_PROFILE = "personal-classify"
REGION = "us-east-1"
MIN_FILE_SIZE = 10_000  # bytes — stub files from before the pipeline was live are ~684 bytes

RESULTS_DIR = Path(__file__).parent
OUTPUT_FILE = RESULTS_DIR / "quantitative_results.json"

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260926

CIRCUIT_DAY_FIELDS = [
    "date", "circuit_id", "zone", "shift",
    "baseline_distance_km", "optimized_distance_km", "distance_improvement_pct",
    "baseline_duration_min", "optimized_duration_min", "duration_improvement_pct",
    "baseline_stops", "optimized_stops", "stops_skipped",
]


def list_daily_files(s3: Any) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=GOLD_DAILY_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json") and obj["Size"] >= MIN_FILE_SIZE:
                keys.append(obj["Key"])
    return sorted(keys)


def load_gold(s3: Any, key: str) -> dict:
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def date_from_key(key: str) -> str:
    return Path(key).stem  # "analytics-results/daily/2026-05-15.json" → "2026-05-15"


def bootstrap_ci(values: list[float], level: float = 0.95) -> tuple[float, float]:
    """IC percentil bootstrap de la media (semilla fija, reproducible)."""
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(values)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    lo = means[int((1 - level) / 2 * BOOTSTRAP_RESAMPLES)]
    hi = means[int((1 + level) / 2 * BOOTSTRAP_RESAMPLES) - 1]
    return round(lo, 2), round(hi, 2)


def paired_wilcoxon(baseline: list[float], optimized: list[float]) -> dict:
    """Wilcoxon signed-rank pareado (H1: optimizado < baseline) + rank-biserial."""
    try:
        from scipy.stats import wilcoxon
    except ImportError:
        return {"error": "scipy not installed (pip install scipy)"}
    diffs = [b - o for b, o in zip(baseline, optimized) if b != o]
    if len(diffs) < 10:
        return {"error": f"only {len(diffs)} non-zero pairs"}
    res = wilcoxon(baseline, optimized, alternative="greater", zero_method="wilcox")
    n = len(diffs)
    total_rank = n * (n + 1) / 2
    # rank-biserial r = (W+ - W-) / total; scipy devuelve W+ para alternative='greater'
    w_plus = float(res.statistic)
    r_rb = (w_plus - (total_rank - w_plus)) / total_rank
    return {
        "n_pairs": n,
        "statistic_w_plus": w_plus,
        "p_value": float(res.pvalue),
        "rank_biserial_r": round(r_rb, 3),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="Primer día a incluir (YYYY-MM-DD, inclusive)")
    ap.add_argument("--until", help="Último día a incluir (YYYY-MM-DD, inclusive)")
    ap.add_argument("--label", help="Sufijo de los archivos de salida (p. ej. interim, final)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=REGION)
    s3 = session.client("s3")

    daily_keys = [
        k for k in list_daily_files(s3)
        if (not args.since or date_from_key(k) >= args.since)
        and (not args.until or date_from_key(k) <= args.until)
    ]
    if not daily_keys:
        print("No daily Gold JSON files found in S3 for the requested range.", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(daily_keys)} daily files ({date_from_key(daily_keys[0])} → {date_from_key(daily_keys[-1])})")

    # One entry per (circuit_id, date) — raw circuit-level data
    circuit_rows: list[dict] = []
    # One entry per date — daily-level summary
    daily_rows: list[dict] = []
    # mean containers per circuit (from top-level by_circuit, collected in the same pass)
    containers_per_circuit: list[float] = []

    for key in daily_keys:
        gold = load_gold(s3, key)
        date = gold.get("date", date_from_key(key))
        route_eff = gold.get("route_efficiency")
        if not route_eff:
            continue

        top_summary = gold.get("summary", {})
        re_summary = route_eff.get("summary", {})

        daily_rows.append({
            "date": date,
            "circuits_with_routes": re_summary.get("circuits_with_routes", 0),
            "containers_reporting": top_summary.get("containers_reporting", 0),
            "avg_fill_level": top_summary.get("avg_fill_level"),
            "avg_distance_improvement_pct": re_summary.get("avg_distance_improvement_pct"),
            "total_distance_saved_km": re_summary.get("total_distance_saved_km"),
            "total_stops_skipped": re_summary.get("total_stops_skipped", 0),
        })

        for circuit in route_eff.get("by_circuit", []):
            if not circuit.get("baseline_distance_km", 0):
                continue
            circuit_rows.append({"date": date, **circuit})

        for entry in gold.get("by_circuit", []):
            n = entry.get("containers_reporting")
            if n:
                containers_per_circuit.append(n)

        print(f"  {date}: {re_summary.get('circuits_with_routes', 0)} circuits, "
              f"avg improvement {re_summary.get('avg_distance_improvement_pct', 0):.1f}%")

    if not circuit_rows:
        print("No route efficiency data found.", file=sys.stderr)
        sys.exit(1)

    # ── Overall route efficiency metrics ────────────────────────────────────
    dist_imp   = [c["distance_improvement_pct"] for c in circuit_rows]
    dur_imp    = [c["duration_improvement_pct"]  for c in circuit_rows]
    base_km    = [c["baseline_distance_km"]       for c in circuit_rows]
    opt_km     = [c["optimized_distance_km"]      for c in circuit_rows]
    base_stops = [c["baseline_stops"]             for c in circuit_rows]
    opt_stops  = [c["optimized_stops"]            for c in circuit_rows]
    skipped    = [c["stops_skipped"]              for c in circuit_rows]
    skipped_pct = [
        c["stops_skipped"] / c["baseline_stops"] * 100
        for c in circuit_rows if c["baseline_stops"] > 0
    ]

    # ── By-shift metrics (derived from per-circuit rows, not from pre-aggregated by_shift) ──
    shift_buckets: dict[str, dict[str, list]] = defaultdict(lambda: {
        "dist_imp": [], "skipped_pct": [], "circuits_per_day": defaultdict(set),
    })
    for c in circuit_rows:
        shift = c.get("shift", "unknown")
        shift_buckets[shift]["dist_imp"].append(c["distance_improvement_pct"])
        if c["baseline_stops"] > 0:
            shift_buckets[shift]["skipped_pct"].append(
                c["stops_skipped"] / c["baseline_stops"] * 100
            )
        shift_buckets[shift]["circuits_per_day"][c["date"]].add(c["circuit_id"])

    by_shift: list[dict] = []
    for shift_name in sorted(shift_buckets):
        m = shift_buckets[shift_name]
        circuits_per_day_counts = [len(v) for v in m["circuits_per_day"].values()]
        by_shift.append({
            "shift": shift_name,
            "avg_circuits_per_day": round(statistics.mean(circuits_per_day_counts), 1),
            "mean_distance_reduction_pct": round(statistics.mean(m["dist_imp"]), 1),
            "mean_containers_skipped_pct": round(statistics.mean(m["skipped_pct"]), 1) if m["skipped_pct"] else None,
        })

    total_base = sum(base_km)
    total_opt  = sum(opt_km)

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_range": {
            "first_date": date_from_key(daily_keys[0]),
            "last_date":  date_from_key(daily_keys[-1]),
            "days_analyzed": len(daily_rows),
            "total_circuit_days": len(circuit_rows),
        },
        "table1_operational_performance": {
            "_note": "Equivalent to paper Table 1 — now computed over full history",
            "total_unique_circuits": len({c["circuit_id"] for c in circuit_rows}),
            "mean_circuits_per_day": round(statistics.mean(r["circuits_with_routes"] for r in daily_rows), 1),
            "total_containers_monitored": int(statistics.mean(
                r["containers_reporting"] for r in daily_rows if r["containers_reporting"] > 0
            )),
            "mean_containers_per_circuit": round(statistics.mean(containers_per_circuit), 1) if containers_per_circuit else None,
            "mean_containers_above_threshold": round(statistics.mean(opt_stops), 1),
            "mean_sequential_baseline_distance_km": round(statistics.mean(base_km), 1),
            "mean_optimized_route_distance_km": round(statistics.mean(opt_km), 1),
            "mean_distance_reduction_pct": round(statistics.mean(dist_imp), 1),
            "median_distance_reduction_pct": round(statistics.median(dist_imp), 1),
            "stddev_distance_reduction_pct": round(statistics.stdev(dist_imp), 1),
            "mean_duration_reduction_pct": round(statistics.mean(dur_imp), 1),
            "mean_containers_skipped_abs": round(statistics.mean(skipped), 1),
            "mean_containers_skipped_pct": round(statistics.mean(skipped_pct), 1),
            "total_distance_saved_km": round(total_base - total_opt, 1),
            "total_baseline_km": round(total_base, 1),
            "total_optimized_km": round(total_opt, 1),
        },
        "table2_by_shift": {
            "_note": "Equivalent to paper Table 2 — per-shift breakdown",
            "shifts": by_shift,
        },
        "paper_comparison": {
            "_note": "Original paper values (5-day average) vs current results",
            "mean_distance_reduction_pct": {
                "paper_5d": 51.0,
                "now": round(statistics.mean(dist_imp), 1),
            },
            "median_distance_reduction_pct": {
                "paper_5d": 51.5,
                "now": round(statistics.median(dist_imp), 1),
            },
            "mean_baseline_distance_km": {
                "paper_5d": 104.7,
                "now": round(statistics.mean(base_km), 1),
            },
            "mean_optimized_distance_km": {
                "paper_5d": 48.5,
                "now": round(statistics.mean(opt_km), 1),
            },
            "mean_containers_skipped_abs": {
                "paper_5d": 9.3,
                "now": round(statistics.mean(skipped), 1),
            },
            "mean_containers_skipped_pct": {
                "paper_5d": 12.7,
                "now": round(statistics.mean(skipped_pct), 1),
            },
            "by_shift_morning_dist_reduction_pct": {
                "paper_5d": 58.7,
                "now": next((s["mean_distance_reduction_pct"] for s in by_shift if s["shift"] == "morning"), None),
            },
            "by_shift_afternoon_dist_reduction_pct": {
                "paper_5d": 48.1,
                "now": next((s["mean_distance_reduction_pct"] for s in by_shift if s["shift"] == "afternoon"), None),
            },
            "by_shift_night_dist_reduction_pct": {
                "paper_5d": 43.8,
                "now": next((s["mean_distance_reduction_pct"] for s in by_shift if s["shift"] == "night"), None),
            },
        },
        "daily_breakdown": daily_rows,
    }

    # ── Incertidumbre y significancia (revisión CAEE: R1, R2, R3, EiC) ─────────
    fills = [r["avg_fill_level"] for r in daily_rows if r.get("avg_fill_level") is not None]
    base_min = [c["baseline_duration_min"] for c in circuit_rows]
    opt_min = [c["optimized_duration_min"] for c in circuit_rows]
    results["statistics"] = {
        "_note": "Unidad de análisis = circuito-día. IC 95% bootstrap percentil de la media "
                 f"({BOOTSTRAP_RESAMPLES} remuestreos, seed {BOOTSTRAP_SEED}). "
                 "Wilcoxon signed-rank pareado baseline vs optimizado, una cola.",
        "n_circuit_days": len(circuit_rows),
        "distance_reduction_pct": {
            "mean": round(statistics.mean(dist_imp), 2),
            "sd": round(statistics.stdev(dist_imp), 2) if len(dist_imp) > 1 else None,
            "median": round(statistics.median(dist_imp), 2),
            "ci95": bootstrap_ci(dist_imp),
        },
        "duration_reduction_pct": {
            "mean": round(statistics.mean(dur_imp), 2),
            "sd": round(statistics.stdev(dur_imp), 2) if len(dur_imp) > 1 else None,
            "median": round(statistics.median(dur_imp), 2),
            "ci95": bootstrap_ci(dur_imp),
        },
        "wilcoxon_distance_km": paired_wilcoxon(base_km, opt_km),
        "wilcoxon_duration_min": paired_wilcoxon(base_min, opt_min),
        "observed_mean_fill_pct": {
            "mean_of_daily_means": round(statistics.mean(fills), 1) if fills else None,
            "min_daily": round(min(fills), 1) if fills else None,
            "max_daily": round(max(fills), 1) if fills else None,
        },
    }

    suffix = f"_{args.label}" if args.label else ""
    out_json = RESULTS_DIR / f"quantitative_results{suffix}.json"
    out_json.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults written to: {out_json}")

    out_csv = RESULTS_DIR / f"circuit_days{suffix}.csv"
    with out_csv.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CIRCUIT_DAY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(circuit_rows)
    print(f"Circuit-day rows written to: {out_csv}")

    out_md = RESULTS_DIR / f"summary{suffix}.md"
    out_md.write_text(render_summary_md(results, args))
    print(f"Summary written to: {out_md}")

    # ── Console summary ──────────────────────────────────────────────────────
    t1 = results["table1_operational_performance"]
    print(f"\n{'='*60}")
    print(f"QUANTITATIVE RESULTS — {results['data_range']['days_analyzed']} days "
          f"({results['data_range']['first_date']} → {results['data_range']['last_date']})")
    print(f"{'='*60}")
    print(f"Unique circuits evaluated:      {t1['total_unique_circuits']}  (paper: 133)")
    print(f"Containers monitored:           {t1['total_containers_monitored']:,}  (paper: 10,937)")
    print(f"Mean containers/circuit:        {t1['mean_containers_per_circuit']}  (paper: 81.6)")
    print(f"Mean containers above threshold:{t1['mean_containers_above_threshold']}  (paper: 71.4)")
    print(f"Mean baseline distance:         {t1['mean_sequential_baseline_distance_km']} km  (paper: 104.7 km)")
    print(f"Mean optimized distance:        {t1['mean_optimized_route_distance_km']} km  (paper: 48.5 km)")
    print(f"Mean distance reduction:        {t1['mean_distance_reduction_pct']}%  (paper: 51.0%)")
    print(f"Median distance reduction:      {t1['median_distance_reduction_pct']}%  (paper: 51.5%)")
    print(f"Mean containers skipped:        {t1['mean_containers_skipped_abs']} ({t1['mean_containers_skipped_pct']}%)  (paper: 9.3, 12.7%)")
    print(f"\nBy shift:")
    for s in results["table2_by_shift"]["shifts"]:
        print(f"  {s['shift']:12s}: {s['mean_distance_reduction_pct']:5.1f}% reduction, "
              f"{s['mean_containers_skipped_pct']}% skipped  "
              f"(~{s['avg_circuits_per_day']} circuits/day)")


def render_summary_md(results: dict, args: argparse.Namespace) -> str:
    """Resumen corto en markdown para compartir resultados intermedios."""
    dr = results["data_range"]
    t1 = results["table1_operational_performance"]
    st = results["statistics"]
    d, t = st["distance_reduction_pct"], st["duration_reduction_pct"]
    wd = st["wilcoxon_distance_km"]
    fill = st["observed_mean_fill_pct"]
    lines = [
        f"# SmartWaste MVD — pipeline results ({args.label or 'all'})",
        "",
        f"- Range: {dr['first_date']} → {dr['last_date']} ({dr['days_analyzed']} days, "
        f"{dr['total_circuit_days']} circuit-days, {t1['total_unique_circuits']} circuits)",
        f"- Observed mean fill level: {fill['mean_of_daily_means']}% "
        f"(daily range {fill['min_daily']}–{fill['max_daily']}%)",
        "",
        "| Metric | Mean ± SD | Median | 95% CI (bootstrap) |",
        "|---|---|---|---|",
        f"| Distance reduction (%) ↑ | {d['mean']} ± {d['sd']} | {d['median']} | [{d['ci95'][0]}, {d['ci95'][1]}] |",
        f"| Travel-time reduction (%) ↑ | {t['mean']} ± {t['sd']} | {t['median']} | [{t['ci95'][0]}, {t['ci95'][1]}] |",
        "",
        f"- Mean static baseline: {t1['mean_sequential_baseline_distance_km']} km; "
        f"mean optimized: {t1['mean_optimized_route_distance_km']} km",
        f"- Containers skipped per circuit-day: {t1['mean_containers_skipped_abs']} "
        f"({t1['mean_containers_skipped_pct']}%)",
        f"- Wilcoxon (baseline km > optimized km): "
        + (f"W+={wd['statistic_w_plus']:.0f}, p={wd['p_value']:.2e}, r_rb={wd['rank_biserial_r']}, n={wd['n_pairs']}"
           if "p_value" in wd else wd.get("error", "n/a")),
        "",
        "## By shift",
        "",
        "| Shift | Circuits/day | Distance reduction (%) ↑ | Skipped (%) |",
        "|---|---|---|---|",
    ]
    for s in results["table2_by_shift"]["shifts"]:
        lines.append(f"| {s['shift']} | {s['avg_circuits_per_day']} | "
                     f"{s['mean_distance_reduction_pct']} | {s['mean_containers_skipped_pct']} |")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
