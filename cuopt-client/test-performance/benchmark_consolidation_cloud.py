"""
benchmark_consolidation_cloud.py — SmartWaste MVD

Mide el beneficio de enviar varios circuitos consolidados en UN solo solve
a cuOpt (cloud API) vs. resolver cada circuito por separado (estilo OR-Tools
secuencial pre-consolidación).

Reemplaza y unifica los antiguos:
  - experiment_consolidation.py        (3, 5 circuitos)
  - experiment_megaconsolidation.py    (5, 8, 10, 12 circuitos)

Cubre el rango "cloud": tamaños que entran dentro del límite de ~1,000 nodos
de la API gratuita de NVIDIA cuOpt. Para grupos más grandes ver
`benchmark_consolidation_selfhosted.py` (requiere EC2 g5.2xlarge).

Tres modos comparados sobre el mismo conjunto de circuitos sintéticos:

  A. cuOpt CONSOLIDATED   → un único solve con todos los containers
                            del grupo + flota agrupada. cuOpt decide
                            asignación cross-circuit.
  B. ORTools CONSOLIDATED → mismo problema consolidado pero resuelto
                            con OR-Tools (apples-to-apples).
  C. ORTools SEQUENTIAL   → un solve independiente por circuito (cada
                            uno con su propio depósito + flota). Simula
                            el comportamiento del handler PRE-consolidación.

Métricas reportadas: wall-clock total, cost del solver, rutas activas,
Δ% calidad relativa.

Uso:
  export CUOPT_API_KEY=<nvapi-key>
  python3 -u benchmark_consolidation_cloud.py                       # default: 3,5,8,10,12
  python3 -u benchmark_consolidation_cloud.py --group-sizes 3,5     # rango "small" (ex-consolidation)
  python3 -u benchmark_consolidation_cloud.py --group-sizes 5,8,10,12  # rango "medium" (ex-megaconsolidation)
  python3 -u benchmark_consolidation_cloud.py --time-limit 30 --seed-base 9000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vrp_solver import CuOptSolver, ORToolsSolver  # type: ignore
from problem_generator import ProblemGenerator, VRPProblem  # type: ignore

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────

CIRCUIT_SIZE        = 80    # ~promedio Montevideo (82)
TRUCKS_PER_CIRCUIT  = 2     # ~promedio MVD (1.7)
DEFAULT_GROUP_SIZES = [3, 5, 8, 10, 12]   # cubre ambos rangos previos
DEFAULT_TIME_LIMIT  = 10
DEFAULT_SEED_BASE   = 1000

OUT_DIR = Path("results")
OUT_DIR.mkdir(exist_ok=True)


@dataclass
class RunResult:
    mode: str           # "cuopt_consolidated" | "ortools_consolidated" | "ortools_sequential"
    group_size: int
    total_nodes: int
    total_vehicles: int
    wall_clock_s: float
    total_cost: float
    status: str
    routes_used: int


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _generate_circuits(g: int, seed_base: int) -> list[VRPProblem]:
    """Genera g problemas independientes (uno por circuito sintético)."""
    gen = ProblemGenerator()
    return [
        gen.generate(num_nodes=CIRCUIT_SIZE, num_vehicles=TRUCKS_PER_CIRCUIT,
                     seed=seed_base + i)
        for i in range(g)
    ]


def _build_consolidated(circuits: list[VRPProblem]) -> VRPProblem:
    """
    Combina g circuitos en un único VRPProblem consolidado:
      - Mismo depósito (el del primer circuito)
      - Concatena todos los containers
      - Pool de vehículos (suma capacidades)
      - Cost matrix recomputada para el set consolidado vía haversine
    """
    gen = ProblemGenerator()
    coords = [circuits[0].coordinates[0]]   # depot compartido
    demands = [0.0]
    capacities_pool: list[float] = []
    for c in circuits:
        coords.extend(c.coordinates[1:])
        demands.extend(c.demands[1:])
        capacities_pool.extend(c.capacities)

    cost_matrix = gen._build_cost_matrix(coords)
    return VRPProblem(
        name=f"consolidated_{len(circuits)}c",
        num_nodes=len(coords),
        num_vehicles=len(capacities_pool),
        cost_matrix=cost_matrix,
        demands=demands,
        capacities=capacities_pool,
        depot_start_idx=0,
        depot_end_idx=0,
        coordinates=coords,
    )


def _solve_once(solver, problem: VRPProblem, tl: int) -> tuple[float, str, float, int]:
    """Returns (wall_s, status, cost, routes_used)."""
    start = time.perf_counter()
    try:
        r = solver.solve_vrp(
            cost_matrix=problem.cost_matrix,
            num_vehicles=problem.num_vehicles,
            demands=problem.demands,
            capacities=problem.capacities,
            depot_start_idx=problem.depot_start_idx,
            depot_end_idx=problem.depot_end_idx,
            time_limit=tl,
        )
        wall = time.perf_counter() - start
        routes = r.get("routes", {})
        routes_used = sum(1 for v in routes.values() if len(v) > 2) if isinstance(routes, dict) else 0
        return wall, r.get("status", "UNK"), float(r.get("total_cost", 0)), routes_used
    except Exception as e:
        wall = time.perf_counter() - start
        return wall, f"ERR:{str(e)[:80]}", 0.0, 0


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def _parse_int_list(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group-sizes", default=",".join(str(x) for x in DEFAULT_GROUP_SIZES),
                        help=f"Tamaños de grupo a probar (default: {DEFAULT_GROUP_SIZES})")
    parser.add_argument("--time-limit", type=int, default=DEFAULT_TIME_LIMIT,
                        help=f"Time limit del solver en segundos (default: {DEFAULT_TIME_LIMIT})")
    parser.add_argument("--seed-base", type=int, default=DEFAULT_SEED_BASE,
                        help=f"Seed base para reproducibilidad (default: {DEFAULT_SEED_BASE})")
    args = parser.parse_args()

    group_sizes = _parse_int_list(args.group_sizes)
    tl = args.time_limit
    seed_base = args.seed_base

    if not os.environ.get("CUOPT_API_KEY"):
        print("⚠ CUOPT_API_KEY no está set en env. Abortando.")
        sys.exit(1)

    print(f"benchmark_consolidation_cloud — group_sizes={group_sizes}, TL={tl}s, seed_base={seed_base}\n")

    cu  = CuOptSolver(mode="api_catalog")
    ort = ORToolsSolver()

    runs: list[RunResult] = []

    print(f"{'group':>5} {'mode':>22} {'nodes':>6} {'veh':>4} {'wall(s)':>8} {'cost':>10} {'rt':>3} status")
    print("-" * 82)

    for g in group_sizes:
        circuits = _generate_circuits(g, seed_base)
        consolidated = _build_consolidated(circuits)
        total_nodes = consolidated.num_nodes
        total_vehs  = consolidated.num_vehicles
        print(f"\n## g={g} circuitos = {total_nodes} nodos, {total_vehs} trucks pooled")

        # A. cuOpt consolidated
        w, st, c, ru = _solve_once(cu, consolidated, tl)
        runs.append(RunResult("cuopt_consolidated", g, total_nodes, total_vehs, w, c, st, ru))
        print(f"{g:>5} {'cuopt_consolidated':>22} {total_nodes:>6} {total_vehs:>4} {w:>7.1f}s {int(c):>10} {ru:>3} {st}")

        # B. OR-Tools consolidated
        w, st, c, ru = _solve_once(ort, consolidated, tl)
        runs.append(RunResult("ortools_consolidated", g, total_nodes, total_vehs, w, c, st, ru))
        print(f"{g:>5} {'ortools_consolidated':>22} {total_nodes:>6} {total_vehs:>4} {w:>7.1f}s {int(c):>10} {ru:>3} {st}")

        # C. OR-Tools sequential (g solves, sum)
        total_wall = 0.0
        total_cost_seq = 0.0
        total_routes = 0
        total_v = 0
        last_status = "OPTIMAL"
        for p in circuits:
            w, st, c, ru = _solve_once(ort, p, tl)
            total_wall += w
            total_cost_seq += c
            total_routes += ru
            total_v += p.num_vehicles
            if not (st.startswith("OPTIMAL") or st.startswith("FEASIBLE")):
                last_status = st
        runs.append(RunResult("ortools_sequential", g, total_nodes, total_v,
                              total_wall, total_cost_seq, last_status, total_routes))
        print(f"{g:>5} {'ortools_sequential':>22} {total_nodes:>6} {total_v:>4} "
              f"{total_wall:>7.1f}s {int(total_cost_seq):>10} {total_routes:>3} {last_status}")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"consolidation_cloud_{ts}.json"
    with open(out_path, "w") as f:
        json.dump([asdict(r) for r in runs], f, indent=2)
    print(f"\nRaw JSON: {out_path}")

    # Comparative table
    print("\n" + "=" * 88)
    print("  COMPARATIVA POR GROUP SIZE — Consolidation (cloud)")
    print("=" * 88)
    print(f"{'group':>5} {'nodes':>6} {'metric':>25} {'cuopt_cons':>11} {'ort_cons':>11} {'ort_seq':>11}")
    print("-" * 88)
    for g in group_sizes:
        cu_c = next((r for r in runs if r.group_size == g and r.mode == "cuopt_consolidated"), None)
        or_c = next((r for r in runs if r.group_size == g and r.mode == "ortools_consolidated"), None)
        or_s = next((r for r in runs if r.group_size == g and r.mode == "ortools_sequential"), None)
        if not (cu_c and or_c and or_s):
            continue

        n = cu_c.total_nodes
        print(f"{g:>5} {n:>6} {'wall_clock_s':>25} {cu_c.wall_clock_s:>10.1f}s {or_c.wall_clock_s:>10.1f}s {or_s.wall_clock_s:>10.1f}s")
        print(f"{g:>5} {n:>6} {'total_cost':>25} {int(cu_c.total_cost):>11} {int(or_c.total_cost):>11} {int(or_s.total_cost):>11}")
        print(f"{g:>5} {n:>6} {'routes_used':>25} {cu_c.routes_used:>11} {or_c.routes_used:>11} {or_s.routes_used:>11}")
        if cu_c.total_cost > 0:
            d_or_c = (or_c.total_cost - cu_c.total_cost) / cu_c.total_cost * 100 if or_c.total_cost > 0 else None
            d_or_s = (or_s.total_cost - cu_c.total_cost) / cu_c.total_cost * 100 if or_s.total_cost > 0 else None
            d_or_c_s = f"{d_or_c:+10.2f}%" if d_or_c is not None else "—"
            d_or_s_s = f"{d_or_s:+10.2f}%" if d_or_s is not None else "—"
            print(f"{g:>5} {n:>6} {'cost Δ% vs cuopt_cons':>25} {'—':>11} {d_or_c_s:>11} {d_or_s_s:>11}")
        if cu_c.wall_clock_s > 0:
            sp = or_s.wall_clock_s / cu_c.wall_clock_s
            print(f"{g:>5} {n:>6} {'wall speedup cu vs or_seq':>25} {sp:>10.2f}x {'—':>11} {'—':>11}")
        print()


if __name__ == "__main__":
    main()
