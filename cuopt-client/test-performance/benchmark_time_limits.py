"""
benchmark_time_limits.py — SmartWaste MVD

Verifica si cuOpt termina antes del time_limit cuando ya encontró la solución
óptima, o si siempre agota el presupuesto completo.

Testea time_limits = [1, 2, 3, 5, 10] contra el API Catalog de NVIDIA,
para los 4 tamaños de problema (50–200 nodos), 1 vehículo.

Para cada combinación reporta:
  - latencia wall-clock real (¿termina antes del time_limit?)
  - status: OPTIMAL vs FEASIBLE
  - total_cost (para ver si la solución mejora con más tiempo)

Uso:
  python benchmark_time_limits.py
  python benchmark_time_limits.py --time-limits 1 3 5 10
  python benchmark_time_limits.py --sizes 50 100 --vehicles 2
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vrp_solver import CuOptSolver

from config import RESULTS_DIR
from problem_generator import ProblemGenerator, VRPProblem
from result_collector import RequestResult, ResultCollector

logging.basicConfig(
    level=logging.WARNING,  # silenciar INFO del solver para output limpio
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_TIME_LIMITS = [3, 5, 7, 9, 11]
DEFAULT_SIZES       = [100, 150, 200, 300, 400, 500]
DEFAULT_VEHICLES    = [1]
DEFAULT_REPS        = 1  # 1 rep por defecto (cada llamada cuesta 1 req del free tier)


def solve_timed(
    solver: CuOptSolver,
    problem: VRPProblem,
    time_limit: int,
    run_index: int,
) -> RequestResult:
    """Ejecuta un solve y mide latencia wall-clock real."""
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
        return RequestResult(
            problem_name=problem.name,
            mode=f"api_catalog_tl{time_limit}s",
            scenario="sequential",
            concurrency_level=1,
            num_nodes=problem.num_nodes,
            num_vehicles=problem.num_vehicles,
            latency_ms=latency_ms,
            success=result["status"] in ("OPTIMAL", "FEASIBLE"),
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
            mode=f"api_catalog_tl{time_limit}s",
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


def print_results_table(results: list[RequestResult], time_limits: list[int]) -> None:
    """
    Imprime tabla comparativa:
      Filas    = tamaño de problema
      Columnas = time_limit
      Celdas   = latencia real (ms) | status | cost
    """
    sizes    = sorted({r.num_nodes for r in results})
    vehicles = sorted({r.num_vehicles for r in results})

    for v in vehicles:
        print(f"\n{'═'*80}")
        print(f"  Vehículos = {v}  —  ¿Termina cuOpt antes del time_limit?")
        print(f"{'═'*80}")

        # Encabezado
        hdr = f"  {'Nodos':>5}"
        for tl in time_limits:
            hdr += f"  {'TL='+str(tl)+'s':^30}"
        print(hdr)
        print(f"  {'':>5}" + "  " + "  ".join(f"{'lat(ms) / status / cost':^30}" for _ in time_limits))
        print(f"  {'─'*76}")

        for size in sizes:
            row = f"  {size:>5}"
            for tl in time_limits:
                match = [
                    r for r in results
                    if r.num_nodes == size
                    and r.num_vehicles == v
                    and f"tl{tl}s" in r.mode
                ]
                if match:
                    r = match[0]
                    early = "⚡" if r.latency_ms < tl * 1000 * 0.90 else "  "
                    cell = f"{r.latency_ms:>6.0f}ms {early}{r.status:<9} {int(r.total_cost):>7}"
                    row += f"  {cell:<30}"
                else:
                    row += f"  {'—':^30}"
            print(row)

        print()
        print("  ⚡ = terminó más de un 10% antes del time_limit (early exit)")
        print()

        # Tabla de costos normalizada al time_limit=10s para ver si la calidad mejora
        ref_results = {r.num_nodes: r for r in results if f"tl{max(time_limits)}s" in r.mode and r.num_vehicles == v}
        if ref_results:
            print(f"  Costo relativo al time_limit máximo ({max(time_limits)}s):")
            hdr2 = f"  {'Nodos':>5}" + "".join(f"  {'TL='+str(tl)+'s':>10}" for tl in time_limits)
            print(hdr2)
            for size in sizes:
                ref = ref_results.get(size)
                if not ref or ref.total_cost == 0:
                    continue
                row2 = f"  {size:>5}"
                for tl in time_limits:
                    match = [
                        r for r in results
                        if r.num_nodes == size and r.num_vehicles == v and f"tl{tl}s" in r.mode
                    ]
                    if match and match[0].total_cost > 0:
                        pct = (match[0].total_cost / ref.total_cost - 1) * 100
                        sign = "+" if pct > 0 else ""
                        row2 += f"  {sign}{pct:>+.1f}%{' ':>4}"
                    else:
                        row2 += f"  {'—':>10}"
                print(row2)
            print("  (0% = igual que con el tiempo máximo; +X% = peor solución)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Testa si cuOpt termina antes del time_limit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--time-limits", nargs="+", type=int, default=DEFAULT_TIME_LIMITS,
        metavar="T", help=f"Time limits a testear en segundos (default: {DEFAULT_TIME_LIMITS})",
    )
    parser.add_argument(
        "--sizes", nargs="+", type=int, default=DEFAULT_SIZES,
        metavar="N", help=f"Tamaños de problema en nodos (default: {DEFAULT_SIZES})",
    )
    parser.add_argument(
        "--vehicles", nargs="+", type=int, default=DEFAULT_VEHICLES,
        metavar="V", help=f"Cantidades de vehículos (default: {DEFAULT_VEHICLES})",
    )
    parser.add_argument(
        "--reps", type=int, default=DEFAULT_REPS,
        help=f"Repeticiones por configuración (default: {DEFAULT_REPS})",
    )
    parser.add_argument(
        "--containers-csv", default=None,
        help="CSV de contenedores reales",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Semilla (default: 42)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    total_calls = len(args.time_limits) * len(args.sizes) * len(args.vehicles) * args.reps
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 60)
    print("  cuOpt Time-Limit Sensitivity — SmartWaste MVD")
    print("=" * 60)
    print(f"  Time limits : {args.time_limits}s")
    print(f"  Sizes       : {args.sizes} nodos")
    print(f"  Vehicles    : {args.vehicles}")
    print(f"  Reps        : {args.reps}")
    print(f"  Total calls : {total_calls}  (API Catalog free tier: 5000/mes)")
    print(f"  Run ID      : {run_id}")
    print("=" * 60)
    print()

    generator = ProblemGenerator(containers_csv=args.containers_csv)
    solver    = CuOptSolver(mode="api_catalog")

    all_results: list[RequestResult] = []
    run_index = 0

    for tl in args.time_limits:
        print(f"── time_limit = {tl}s ──────────────────────────────────")
        for size in args.sizes:
            for v in args.vehicles:
                problem = generator.generate(num_nodes=size, num_vehicles=v, seed=args.seed)
                for _ in range(args.reps):
                    r = solve_timed(solver, problem, tl, run_index)
                    run_index += 1
                    early = " ⚡EARLY" if r.latency_ms < tl * 1000 * 0.90 else ""
                    print(
                        f"  {size:>3}n/{v}v  "
                        f"tl={tl:>2}s  "
                        f"lat={r.latency_ms:>7.0f}ms  "
                        f"status={r.status:<9}  "
                        f"cost={int(r.total_cost):>8}"
                        f"{early}"
                    )
                    all_results.append(r)

    # Persistir
    output_dir = Path(RESULTS_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    collector = ResultCollector(str(output_dir))
    collector.save_raw_results(all_results, f"timelimit_{run_id}")

    # Tabla comparativa
    print_results_table(all_results, args.time_limits)

    print(f"Raw JSON guardado en: {output_dir}/raw_timelimit_{run_id}.json")


if __name__ == "__main__":
    main()
