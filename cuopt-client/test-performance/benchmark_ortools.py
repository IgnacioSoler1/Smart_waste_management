"""
benchmark_ortools.py — SmartWaste MVD

Benchmark de Google OR-Tools (CPU fallback) para comparar con cuOpt.

Mide latencia y calidad de solución para distintas combinaciones de:
  - problem_sizes: 50, 100, 150, 200 nodos
  - vehicle_counts: 1, 2, 3 vehículos
  - time_limits: 5, 10, 30 segundos (presupuesto del solver)

Los resultados se guardan en el mismo formato CSV que los benchmarks de cuOpt
(summary_<run_id>.csv) para facilitar la comparación directa.

Uso:
  # Run completo (todas las combinaciones × 3 repeticiones)
  python benchmark_ortools.py

  # Tamaños y vehículos personalizados
  python benchmark_ortools.py --sizes 50 100 --vehicles 1 2 --reps 5

  # Solo un time_limit
  python benchmark_ortools.py --time-limits 10

  # Modo rápido (subset reducido, 1 repetición)
  python benchmark_ortools.py --quick
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vrp_solver import ORToolsSolver

from config import RESULTS_DIR
from problem_generator import ProblemGenerator, VRPProblem
from result_collector import RequestResult, ResultCollector, _percentile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuración por defecto ────────────────────────────────────────────────

DEFAULT_SIZES        = [100, 150, 200, 300, 400, 500]
DEFAULT_VEHICLES     = [1]
DEFAULT_TIME_LIMITS  = [3, 5, 7, 9, 11]
DEFAULT_REPETITIONS  = 1

QUICK_SIZES          = [100, 200, 500]
QUICK_VEHICLES       = [1]
QUICK_TIME_LIMITS    = [11]
QUICK_REPETITIONS    = 1


# ── Resultados extendidos (incluye optimal_rate) ─────────────────────────────

@dataclass
class OrtoolsStats:
    """Estadísticas para una combinación (time_limit, num_nodes, num_vehicles)."""

    mode: str         # "ortools_<time_limit>s"
    scenario: str     # siempre "sequential"
    time_limit_s: int
    num_nodes: int
    num_vehicles: int
    concurrency: int  # siempre 1
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    min_ms: float
    max_ms: float
    std_ms: float
    throughput_rps: float
    success_rate: float
    optimal_rate: float  # fracción de runs que devolvieron OPTIMAL (vs FEASIBLE)


# ── Lógica de benchmark ──────────────────────────────────────────────────────

def solve_once(
    solver: ORToolsSolver,
    problem: VRPProblem,
    time_limit: int,
    run_index: int,
) -> RequestResult:
    """Ejecuta un solve midiendo latencia wall-clock."""
    mode_name = f"ortools_{time_limit}s"
    start = time.perf_counter()
    try:
        result = solver.solve_vrp(
            cost_matrix=problem.cost_matrix,
            num_vehicles=problem.num_vehicles,
            demands=problem.demands,
            capacities=problem.capacities,
            depot_start_idx=problem.depot_start_idx,
            depot_end_idx=problem.depot_end_idx,
            time_limit=time_limit,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        success = result["status"] in ("OPTIMAL", "FEASIBLE")
        return RequestResult(
            problem_name=problem.name,
            mode=mode_name,
            scenario="sequential",
            concurrency_level=1,
            num_nodes=problem.num_nodes,
            num_vehicles=problem.num_vehicles,
            latency_ms=latency_ms,
            success=success,
            status=result["status"],
            total_cost=result.get("total_cost", 0),
            error_message=None,
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            request_index=run_index,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return RequestResult(
            problem_name=problem.name,
            mode=mode_name,
            scenario="sequential",
            concurrency_level=1,
            num_nodes=problem.num_nodes,
            num_vehicles=problem.num_vehicles,
            latency_ms=latency_ms,
            success=False,
            status="ERROR",
            total_cost=0,
            error_message=str(exc)[:300],
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            request_index=run_index,
        )


def aggregate_ortools(results: list[RequestResult]) -> list[OrtoolsStats]:
    """Agrupa resultados y calcula estadísticas incluyendo optimal_rate."""
    groups: dict[tuple, list[RequestResult]] = {}
    for r in results:
        key = (r.mode, r.num_nodes, r.num_vehicles)
        groups.setdefault(key, []).append(r)

    stats_list: list[OrtoolsStats] = []
    for (mode, num_nodes, num_vehicles), group in sorted(groups.items()):
        latencies   = [r.latency_ms for r in group]
        successes   = [r for r in group if r.success]
        optimals    = [r for r in group if r.status == "OPTIMAL"]
        sorted_lat  = sorted(latencies)
        time_limit_s = int(mode.split("_")[1].replace("s", ""))

        total_time_s = sum(latencies) / 1000.0
        throughput   = len(successes) / total_time_s if total_time_s > 0 else 0.0
        std          = statistics.stdev(latencies) if len(latencies) > 1 else 0.0

        stats_list.append(OrtoolsStats(
            mode=mode,
            scenario="sequential",
            time_limit_s=time_limit_s,
            num_nodes=num_nodes,
            num_vehicles=num_vehicles,
            concurrency=1,
            count=len(group),
            p50_ms=_percentile(sorted_lat, 50),
            p95_ms=_percentile(sorted_lat, 95),
            p99_ms=_percentile(sorted_lat, 99),
            mean_ms=statistics.mean(latencies),
            min_ms=min(latencies),
            max_ms=max(latencies),
            std_ms=std,
            throughput_rps=round(throughput, 4),
            success_rate=len(successes) / len(group),
            optimal_rate=len(optimals) / len(group),
        ))

    return stats_list


def save_ortools_csv(stats: list[OrtoolsStats], run_id: str, output_dir: Path) -> Path:
    """Guarda estadísticas en CSV."""
    path = output_dir / f"ortools_{run_id}.csv"
    fieldnames = [
        "mode", "scenario", "time_limit_s", "num_nodes", "num_vehicles",
        "concurrency", "count",
        "p50_ms", "p95_ms", "p99_ms", "mean_ms", "min_ms", "max_ms", "std_ms",
        "throughput_rps", "success_rate", "optimal_rate",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in stats:
            row = asdict(s)
            for k in row:
                if isinstance(row[k], float):
                    row[k] = round(row[k], 2)
            writer.writerow(row)
    return path


def print_summary_table(stats: list[OrtoolsStats]) -> None:
    """Imprime tabla comparativa por time_limit y tamaño de problema."""
    time_limits = sorted({s.time_limit_s for s in stats})
    sizes       = sorted({s.num_nodes for s in stats})
    vehicles    = sorted({s.num_vehicles for s in stats})

    for v in vehicles:
        print(f"\n{'─'*72}")
        print(f"  Vehículos = {v}")
        print(f"{'─'*72}")
        header = f"  {'Nodos':>6}  " + "  ".join(
            f"TL={tl:>2}s  p50(ms)  OPTIMAL%" for tl in time_limits
        )
        print(header)
        print(f"  {'─'*68}")

        for size in sizes:
            row_parts = [f"  {size:>6}"]
            for tl in time_limits:
                match = [
                    s for s in stats
                    if s.time_limit_s == tl and s.num_nodes == size and s.num_vehicles == v
                ]
                if match:
                    s = match[0]
                    row_parts.append(
                        f"  {tl:>5}s  {s.p50_ms:>8.0f}  {s.optimal_rate*100:>7.0f}%"
                    )
                else:
                    row_parts.append(f"  {'—':>5}   {'—':>8}   {'—':>7}")
            print("".join(row_parts))


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark de Google OR-Tools para SmartWaste MVD",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sizes", nargs="+", type=int, default=None,
        metavar="N", help=f"Tamaños de problema en nodos (default: {DEFAULT_SIZES})",
    )
    parser.add_argument(
        "--vehicles", nargs="+", type=int, default=None,
        metavar="V", help=f"Cantidades de vehículos (default: {DEFAULT_VEHICLES})",
    )
    parser.add_argument(
        "--time-limits", nargs="+", type=int, default=None,
        metavar="T", help=f"Límites de tiempo en segundos (default: {DEFAULT_TIME_LIMITS})",
    )
    parser.add_argument(
        "--reps", type=int, default=None,
        help=f"Repeticiones por configuración (default: {DEFAULT_REPETITIONS})",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="Modo rápido: subset reducido de configuraciones, 1 repetición",
    )
    parser.add_argument(
        "--containers-csv", default=None,
        help="CSV de contenedores reales (omitir = coordenadas sintéticas de Montevideo)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Semilla para reproducibilidad (default: 42)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.quick:
        sizes       = QUICK_SIZES
        vehicles    = QUICK_VEHICLES
        time_limits = QUICK_TIME_LIMITS
        reps        = QUICK_REPETITIONS
    else:
        sizes       = args.sizes       or DEFAULT_SIZES
        vehicles    = args.vehicles    or DEFAULT_VEHICLES
        time_limits = args.time_limits or DEFAULT_TIME_LIMITS
        reps        = args.reps        or DEFAULT_REPETITIONS

    run_id     = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(RESULTS_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    total_runs = len(sizes) * len(vehicles) * len(time_limits) * reps
    print("=" * 60)
    print("  OR-Tools Benchmark — SmartWaste MVD")
    print("=" * 60)
    print(f"  Sizes       : {sizes}")
    print(f"  Vehicles    : {vehicles}")
    print(f"  Time limits : {time_limits}s")
    print(f"  Repetitions : {reps}")
    print(f"  Total runs  : {total_runs}")
    print(f"  Run ID      : {run_id}")
    print("=" * 60)

    generator = ProblemGenerator(containers_csv=args.containers_csv)
    solver    = ORToolsSolver()

    all_results: list[RequestResult] = []
    run_index = 0

    for tl in time_limits:
        print(f"\n── time_limit = {tl}s ──────────────────────────────────")
        for size in sizes:
            for v in vehicles:
                problem = generator.generate(num_nodes=size, num_vehicles=v, seed=args.seed)
                latencies: list[float] = []
                statuses:  list[str]   = []

                for _ in range(reps):
                    r = solve_once(solver, problem, tl, run_index)
                    all_results.append(r)
                    latencies.append(r.latency_ms)
                    statuses.append(r.status)
                    run_index += 1

                mean_lat = statistics.mean(latencies)
                opt_count = statuses.count("OPTIMAL")
                feas_count = statuses.count("FEASIBLE")
                print(
                    f"  {size:>3}n / {v}v  tl={tl}s  "
                    f"mean={mean_lat:>7.0f}ms  "
                    f"OPTIMAL={opt_count}/{reps}  FEASIBLE={feas_count}/{reps}  "
                    f"statuses={statuses}"
                )

    # ── Persistir resultados ─────────────────────────────────────────────────

    collector = ResultCollector(str(output_dir))
    collector.save_raw_results(all_results, f"ortools_{run_id}")

    stats = aggregate_ortools(all_results)
    csv_path = save_ortools_csv(stats, run_id, output_dir)

    # ── Resumen ──────────────────────────────────────────────────────────────

    print_summary_table(stats)

    print(f"\n{'─'*60}")
    print(f"Raw JSON : {output_dir}/raw_ortools_{run_id}.json")
    print(f"CSV      : {csv_path}")
    print(f"{'─'*60}")


if __name__ == "__main__":
    main()
