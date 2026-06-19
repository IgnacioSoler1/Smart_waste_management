"""
benchmark_inflection.py — SmartWaste MVD

Investigación: ¿en qué punto cuOpt empieza a superar significativamente a OR-Tools?

Hipótesis (basadas en literatura NVIDIA cuOpt):
  - cuOpt brilla con MUCHO espacio de búsqueda: flotas grandes + capacidad ajustada
  - Con 1 vehículo y capacidad holgada (TSP-like), OR-Tools' Guided Local Search
    es altamente competitivo (~1% gap o menos)
  - El gap debería crecer con: (a) cantidad de vehículos, (b) tightness de capacidad

Este script genera el mismo problema VRP para ambos solvers pero MANIPULA la
capacidad post-generación para forzar multi-ruta. Compara cost y latencia.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vrp_solver import CuOptSolver, ORToolsSolver  # type: ignore

from problem_generator import ProblemGenerator, VRPProblem  # type: ignore

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)


@dataclass
class Run:
    size: int
    vehicles: int
    capacity_slack: float  # 1.0 = ajustado (= demanda), 1.5 = holgura 50%
    solver: str            # "cuopt" or "ortools"
    time_limit: int
    latency_ms: float
    status: str
    cost: float
    used_vehicles: int


def override_capacity(problem: VRPProblem, slack: float) -> VRPProblem:
    """Reajusta capacidades para que sean tight: cap = total_demand × slack / num_vehicles."""
    total = sum(problem.demands)
    cap = (total * slack) / problem.num_vehicles
    problem.capacities = [cap] * problem.num_vehicles
    return problem


def count_used_routes(routes) -> int:
    """Cuenta cuántos vehículos tienen al menos 1 stop. routes es dict[int, list[int]]."""
    if not routes:
        return 0
    if isinstance(routes, dict):
        return sum(1 for r in routes.values() if len(r) > 2)
    return sum(1 for r in routes if (hasattr(r, "__len__") and len(r) > 2))


def solve_with(solver, problem: VRPProblem, time_limit: int) -> tuple[float, str, float, int]:
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
        latency = (time.perf_counter() - start) * 1000
        return (
            latency,
            result.get("status", "UNKNOWN"),
            float(result.get("total_cost", 0)),
            count_used_routes(result.get("routes", [])),
        )
    except Exception as exc:
        latency = (time.perf_counter() - start) * 1000
        return (latency, f"ERROR:{str(exc)[:60]}", 0.0, 0)


def main() -> None:
    # ── Configuración del experimento ───────────────────────────────
    SIZES        = [200, 500]
    VEHICLES     = [3, 5, 10, 15]
    SLACKS       = [1.05, 1.20]   # 1.05 = muy tight, 1.20 = moderada
    TIME_LIMIT   = 10
    SEED         = 42

    total = len(SIZES) * len(VEHICLES) * len(SLACKS) * 2  # ×2 solvers
    print(f"Configuración: sizes={SIZES}, vehicles={VEHICLES}, slacks={SLACKS}, TL={TIME_LIMIT}s")
    print(f"Total runs: {total} (~{total * 12 // 60}+ min)\n")

    generator = ProblemGenerator()
    cu  = CuOptSolver(mode="api_catalog")
    ort = ORToolsSolver()

    runs: list[Run] = []
    run_idx = 0

    print(f"{'size':>5} {'veh':>4} {'slack':>6} {'solver':>8} {'status':>12} {'cost':>10} {'used':>5} {'lat(ms)':>9}")
    print("-" * 80)

    for size in SIZES:
        for nv in VEHICLES:
            for slack in SLACKS:
                # Generar problema y ajustar capacidad
                p = generator.generate(num_nodes=size, num_vehicles=nv, seed=SEED)
                p = override_capacity(p, slack)
                total_demand = sum(p.demands)
                cap_each = p.capacities[0]

                # OR-Tools
                run_idx += 1
                lat, status, cost, used = solve_with(ort, p, TIME_LIMIT)
                r = Run(size, nv, slack, "ortools", TIME_LIMIT, lat, status, cost, used)
                runs.append(r)
                print(f"{size:>5} {nv:>4} {slack:>6.2f} {'or-tools':>8} {status[:12]:>12} {int(cost):>10} {used:>5} {int(lat):>9}")

                # cuOpt API
                run_idx += 1
                lat, status, cost, used = solve_with(cu, p, TIME_LIMIT)
                r = Run(size, nv, slack, "cuopt", TIME_LIMIT, lat, status, cost, used)
                runs.append(r)
                print(f"{size:>5} {nv:>4} {slack:>6.2f} {'cuopt':>8} {status[:12]:>12} {int(cost):>10} {used:>5} {int(lat):>9}")

    # ── Persistir ─────────────────────────────────────────────────
    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"inflection_{ts}.json"
    with open(out_path, "w") as f:
        json.dump([r.__dict__ for r in runs], f, indent=2)
    print(f"\nRaw JSON guardado en: {out_path}")

    # ── Análisis comparativo ───────────────────────────────────────
    print("\n" + "=" * 80)
    print("  COMPARATIVA: cuOpt vs OR-Tools (Δ% = OR-T worse; +ve favorece cuOpt)")
    print("=" * 80)
    print(f"{'size':>5} {'veh':>4} {'slack':>6} {'cu_cost':>9} {'or_cost':>9} {'Δ%':>7} {'cu_used':>8} {'or_used':>8}")
    print("-" * 70)

    deltas = []
    for size in SIZES:
        for nv in VEHICLES:
            for slack in SLACKS:
                cu_r = next((r for r in runs if r.size == size and r.vehicles == nv and r.capacity_slack == slack and r.solver == "cuopt"), None)
                or_r = next((r for r in runs if r.size == size and r.vehicles == nv and r.capacity_slack == slack and r.solver == "ortools"), None)
                if cu_r and or_r and cu_r.cost > 0 and or_r.cost > 0:
                    delta = (or_r.cost - cu_r.cost) / cu_r.cost * 100
                    deltas.append((size, nv, slack, delta))
                    print(f"{size:>5} {nv:>4} {slack:>6.2f} {int(cu_r.cost):>9} {int(or_r.cost):>9} {delta:>+6.2f}% {cu_r.used_vehicles:>8} {or_r.used_vehicles:>8}")
                else:
                    cu_st = cu_r.status[:8] if cu_r else "—"
                    or_st = or_r.status[:8] if or_r else "—"
                    print(f"{size:>5} {nv:>4} {slack:>6.2f} {'—':>9} {'—':>9} {'—':>7} cu:{cu_st} or:{or_st}")

    if deltas:
        avg = sum(d[3] for d in deltas) / len(deltas)
        print(f"\nΔ% promedio: {avg:+.2f}%   (positivo = cuOpt mejor)")
        print(f"Δ% rango:    {min(d[3] for d in deltas):+.2f}% a {max(d[3] for d in deltas):+.2f}%")

        # Por dimensión
        print("\nPor cantidad de vehículos:")
        for nv in VEHICLES:
            sub = [d[3] for d in deltas if d[1] == nv]
            if sub:
                print(f"  {nv:>3} vehículos: Δ% promedio = {sum(sub)/len(sub):+.2f}%  (n={len(sub)})")

        print("\nPor slack de capacidad:")
        for sl in SLACKS:
            sub = [d[3] for d in deltas if d[2] == sl]
            if sub:
                print(f"  slack={sl}:    Δ% promedio = {sum(sub)/len(sub):+.2f}%  (n={len(sub)})")


if __name__ == "__main__":
    main()
