"""
benchmark_consolidation_selfhosted.py — SmartWaste MVD

Suite extendida de tests contra cuOpt self-hosted (EC2 g5.2xlarge) para
explorar SISTEMÁTICAMENTE dónde cuOpt domina sobre OR-Tools.

Suites incluidas:

  SUITE 0 — Mega-consolidation (original)
    Grupos de 15, 20, 25, 30 circuitos (1186–2371 nodos), TL=10s,
    sin time windows. Tres modos: cuOpt self-hosted consolidado,
    OR-Tools consolidado, OR-Tools sequential.

  SUITE A — Long time budget sweep
    Mismo problema (20 circuitos = 1581 nodos), TL ∈ {10, 30, 60, 120, 180}s.
    Hipótesis: cuOpt sigue mejorando con más tiempo (búsqueda paralela
    explora más); OR-Tools plateauea antes (Guided Local Search converge
    rápido a óptimo local). El gap debería CRECER con el tiempo.

  SUITE B — Time windows (CVRPTW)
    Mismo problema, pero con time windows aleatorias por task (ventanas
    de ~2h dentro de una jornada de 8h). Hipótesis: cuOpt domina mucho
    más cuando hay constraint propagation (es el régimen de sus world
    records en Gehring & Homberger).

  SUITE C — Short time budget cliff
    Mismo problema que SUITE A (1581 nodos) pero TL ∈ {1, 2, 3, 5, 7, 10}s.
    3 seeds por (TL, solver) para reducir varianza. Mide:
      - cost por TL (¿quién baja más rápido al bajar el time budget?)
      - status (¿quién retorna INFEASIBLE primero?)
      - wall_clock real vs time_limit (overhead = wall - tl)
    Hipótesis a verificar: a budgets muy cortos cuOpt sufre por su
    overhead de GPU warmup; OR-Tools puede tener ventaja relativa
    porque encuentra factible en <1s.

Lee el endpoint del cuOpt self-hosted desde:
  1. --server <url>   (CLI arg)
  2. results/.ec2_state.json (escrito por deploy_cuopt_ec2.sh launch)
  3. CUOPT_SERVER_URL env var

Uso:
  ./deploy_cuopt_ec2.sh launch
  python3 -u benchmark_consolidation_selfhosted.py             # corre todo
  python3 -u benchmark_consolidation_selfhosted.py --suite A   # solo suite A
  python3 -u benchmark_consolidation_selfhosted.py --suite B   # solo suite B
  ./deploy_cuopt_ec2.sh teardown                              # !! cleanup
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vrp_solver import CuOptSolver, ORToolsSolver  # type: ignore
from problem_generator import ProblemGenerator, VRPProblem  # type: ignore

logging.basicConfig(level=logging.WARNING)


# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────

CIRCUIT_SIZE       = 80
TRUCKS_PER_CIRCUIT = 2
SEED_BASE          = 3000

# SUITE 0 — mega-consolidation
SUITE0_GROUP_SIZES = [15, 20, 25, 30]
SUITE0_TIME_LIMIT  = 10

# SUITE A — long time budget
SUITEA_GROUP_SIZE  = 20            # 1581 nodos
SUITEA_TIME_LIMITS = [10, 30, 60, 120, 180]

# SUITE B — time windows
SUITEB_GROUP_SIZE  = 20
SUITEB_TIME_LIMIT  = 30            # más tiempo, las TWs son más difíciles
SUITEB_HORIZON_S   = 8 * 3600      # jornada de 8 h (turno completo)
SUITEB_WINDOW_S    = 2 * 3600      # ventana de 2 h por task
SUITEB_SERVICE_S   = 60            # 1 min de servicio por contenedor (vaciado)

# SUITE C — short time budget cliff
SUITEC_GROUP_SIZE  = 20            # 1581 nodos — mismo problema que SUITEA
SUITEC_TIME_LIMITS = [1, 2, 3, 5, 7, 10]
SUITEC_SEEDS       = [3000, 3100, 3200]   # 3 instancias para varianza

OUT_DIR = Path("results")
OUT_DIR.mkdir(exist_ok=True)
STATE_FILE = OUT_DIR / ".ec2_state.json"


@dataclass
class RunResult:
    suite: str
    mode: str
    label: str           # descripción del caso (group_size, TL, etc.)
    total_nodes: int
    total_vehicles: int
    time_limit_s: int
    wall_clock_s: float
    total_cost: float
    status: str
    routes_used: int


# ─────────────────────────────────────────────────────────
# Helpers comunes
# ─────────────────────────────────────────────────────────

def _resolve_server(cli_server: str | None) -> str:
    if cli_server:
        return cli_server
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            ip = state.get("public_ip")
            if ip:
                return f"http://{ip}:5000"
        except Exception:
            pass
    env = os.environ.get("CUOPT_SERVER_URL")
    if env:
        return env
    raise SystemExit(
        "No cuOpt self-hosted endpoint found. Provide --server URL, "
        "or run ./deploy_cuopt_ec2.sh launch first."
    )


def _generate_circuits(g: int, seed_base: int) -> list[VRPProblem]:
    gen = ProblemGenerator()
    return [
        gen.generate(num_nodes=CIRCUIT_SIZE, num_vehicles=TRUCKS_PER_CIRCUIT,
                     seed=seed_base + i)
        for i in range(g)
    ]


def _build_consolidated(circuits: list[VRPProblem]) -> VRPProblem:
    gen = ProblemGenerator()
    coords = [circuits[0].coordinates[0]]
    demands = [0.0]
    capacities_pool: list[float] = []
    for c in circuits:
        coords.extend(c.coordinates[1:])
        demands.extend(c.demands[1:])
        capacities_pool.extend(c.capacities)
    cost_matrix = gen._build_cost_matrix(coords)
    return VRPProblem(
        name=f"super_{len(circuits)}c",
        num_nodes=len(coords),
        num_vehicles=len(capacities_pool),
        cost_matrix=cost_matrix,
        demands=demands,
        capacities=capacities_pool,
        depot_start_idx=0,
        depot_end_idx=0,
        coordinates=coords,
    )


def _generate_time_windows(
    n: int,
    depot_idx: int,
    horizon_s: int,
    window_s: int,
    seed: int,
) -> tuple[list[tuple[int, int]], list[int]]:
    """
    Genera time windows aleatorias por nodo.
      - Depot: ventana ancha [0, horizon].
      - Tasks: centro aleatorio en [window/2, horizon - window/2],
        ventana de ancho window_s.
    Service time uniforme de SUITEB_SERVICE_S por task (0 en depot).
    """
    rng = random.Random(seed)
    tws: list[tuple[int, int]] = []
    svc: list[int] = []
    for i in range(n):
        if i == depot_idx:
            tws.append((0, horizon_s))
            svc.append(0)
        else:
            center = rng.randint(window_s // 2, horizon_s - window_s // 2)
            start = max(0, center - window_s // 2)
            end   = min(horizon_s, center + window_s // 2)
            tws.append((start, end))
            svc.append(SUITEB_SERVICE_S)
    return tws, svc


def _solve_once(
    solver,
    problem: VRPProblem,
    tl: int,
    *,
    time_windows: list[tuple[int, int]] | None = None,
    service_times: list[int] | None = None,
    vehicle_time_windows: list[tuple[int, int]] | None = None,
) -> tuple[float, str, float, int]:
    """Returns (wall_s, status, cost, routes_used)."""
    start = time.perf_counter()
    try:
        kwargs: dict = dict(
            cost_matrix=problem.cost_matrix,
            num_vehicles=problem.num_vehicles,
            demands=problem.demands,
            capacities=problem.capacities,
            depot_start_idx=problem.depot_start_idx,
            depot_end_idx=problem.depot_end_idx,
            time_limit=tl,
        )
        if time_windows is not None:
            kwargs["time_windows"]         = time_windows
            kwargs["service_times"]        = service_times
            kwargs["vehicle_time_windows"] = vehicle_time_windows
        r = solver.solve_vrp(**kwargs)
        wall = time.perf_counter() - start
        routes = r.get("routes", {})
        routes_used = sum(1 for v in routes.values() if len(v) > 2) if isinstance(routes, dict) else 0
        return wall, r.get("status", "UNK"), float(r.get("total_cost", 0)), routes_used
    except Exception as e:
        return (time.perf_counter() - start, f"ERR:{str(e)[:120]}", 0.0, 0)


# ─────────────────────────────────────────────────────────
# SUITE 0 — Mega-consolidation (original)
# ─────────────────────────────────────────────────────────

def run_suite_0(cu: CuOptSolver, ort: ORToolsSolver,
                runs: list[RunResult], skip_or_sequential: bool) -> None:
    print("\n" + "=" * 88)
    print("  SUITE 0 — Mega-consolidation (15–30 circuitos, TL=10s, sin TW)")
    print("=" * 88)
    for g in SUITE0_GROUP_SIZES:
        circuits = _generate_circuits(g, SEED_BASE)
        cons = _build_consolidated(circuits)
        label = f"g={g}"
        print(f"\n## {label} | {cons.num_nodes} nodos, {cons.num_vehicles} trucks pooled")

        # cuOpt self-hosted consolidated
        w, st, c, ru = _solve_once(cu, cons, SUITE0_TIME_LIMIT)
        runs.append(RunResult("0", "cuopt_consolidated_selfhosted", label, cons.num_nodes, cons.num_vehicles, SUITE0_TIME_LIMIT, w, c, st, ru))
        print(f"  cuopt_selfhosted_cons   wall={w:6.1f}s  cost={int(c):>8}  routes={ru:>3}  status={st}")

        # OR-Tools consolidated
        w, st, c, ru = _solve_once(ort, cons, SUITE0_TIME_LIMIT)
        runs.append(RunResult("0", "ortools_consolidated", label, cons.num_nodes, cons.num_vehicles, SUITE0_TIME_LIMIT, w, c, st, ru))
        print(f"  ortools_consolidated    wall={w:6.1f}s  cost={int(c):>8}  routes={ru:>3}  status={st}")

        if not skip_or_sequential:
            total_wall, total_cost_seq, total_routes, total_v = 0.0, 0.0, 0, 0
            last_status = "OPTIMAL"
            for p in circuits:
                w, st, c, ru = _solve_once(ort, p, SUITE0_TIME_LIMIT)
                total_wall += w; total_cost_seq += c; total_routes += ru; total_v += p.num_vehicles
                if not (st.startswith("OPTIMAL") or st.startswith("FEASIBLE")):
                    last_status = st
            runs.append(RunResult("0", "ortools_sequential", label, cons.num_nodes, total_v, SUITE0_TIME_LIMIT, total_wall, total_cost_seq, last_status, total_routes))
            print(f"  ortools_sequential      wall={total_wall:6.1f}s  cost={int(total_cost_seq):>8}  routes={total_routes:>3}  status={last_status}")


# ─────────────────────────────────────────────────────────
# SUITE A — Long time budget sweep
# ─────────────────────────────────────────────────────────

def run_suite_A(cu: CuOptSolver, ort: ORToolsSolver, runs: list[RunResult]) -> None:
    print("\n" + "=" * 88)
    print(f"  SUITE A — Long time budget (mismo problema, TL ∈ {SUITEA_TIME_LIMITS}s)")
    print("=" * 88)
    circuits = _generate_circuits(SUITEA_GROUP_SIZE, SEED_BASE)
    cons = _build_consolidated(circuits)
    print(f"\n## Problema fijo: g={SUITEA_GROUP_SIZE} = {cons.num_nodes} nodos, {cons.num_vehicles} trucks")

    for tl in SUITEA_TIME_LIMITS:
        label = f"TL={tl}s"
        print(f"\n-- {label} --")

        w, st, c, ru = _solve_once(cu, cons, tl)
        runs.append(RunResult("A", "cuopt_consolidated_selfhosted", label, cons.num_nodes, cons.num_vehicles, tl, w, c, st, ru))
        print(f"  cuopt_selfhosted   wall={w:6.1f}s  cost={int(c):>8}  routes={ru:>3}  status={st}")

        w, st, c, ru = _solve_once(ort, cons, tl)
        runs.append(RunResult("A", "ortools_consolidated", label, cons.num_nodes, cons.num_vehicles, tl, w, c, st, ru))
        print(f"  ortools_cons       wall={w:6.1f}s  cost={int(c):>8}  routes={ru:>3}  status={st}")


# ─────────────────────────────────────────────────────────
# SUITE B — Time windows (CVRPTW)
# ─────────────────────────────────────────────────────────

def run_suite_B(cu: CuOptSolver, ort: ORToolsSolver, runs: list[RunResult]) -> None:
    print("\n" + "=" * 88)
    print(f"  SUITE B — Time windows (CVRPTW, ventana de {SUITEB_WINDOW_S//60}min "
          f"en jornada de {SUITEB_HORIZON_S//3600}h, TL={SUITEB_TIME_LIMIT}s)")
    print("=" * 88)
    circuits = _generate_circuits(SUITEB_GROUP_SIZE, SEED_BASE)
    cons = _build_consolidated(circuits)
    print(f"\n## Problema fijo: g={SUITEB_GROUP_SIZE} = {cons.num_nodes} nodos, {cons.num_vehicles} trucks")

    tws, svc = _generate_time_windows(
        n=cons.num_nodes,
        depot_idx=cons.depot_start_idx,
        horizon_s=SUITEB_HORIZON_S,
        window_s=SUITEB_WINDOW_S,
        seed=SEED_BASE + 999,
    )
    veh_tws = [(0, SUITEB_HORIZON_S)] * cons.num_vehicles
    label = f"TW={SUITEB_WINDOW_S//60}min/{SUITEB_HORIZON_S//3600}h"

    # cuOpt self-hosted con TW
    w, st, c, ru = _solve_once(
        cu, cons, SUITEB_TIME_LIMIT,
        time_windows=tws, service_times=svc, vehicle_time_windows=veh_tws,
    )
    runs.append(RunResult("B", "cuopt_consolidated_TW", label, cons.num_nodes, cons.num_vehicles, SUITEB_TIME_LIMIT, w, c, st, ru))
    print(f"  cuopt_selfhosted_TW    wall={w:6.1f}s  cost={int(c):>8}  routes={ru:>3}  status={st}")

    # OR-Tools con TW
    w, st, c, ru = _solve_once(
        ort, cons, SUITEB_TIME_LIMIT,
        time_windows=tws, service_times=svc, vehicle_time_windows=veh_tws,
    )
    runs.append(RunResult("B", "ortools_consolidated_TW", label, cons.num_nodes, cons.num_vehicles, SUITEB_TIME_LIMIT, w, c, st, ru))
    print(f"  ortools_consolidated_TW wall={w:6.1f}s  cost={int(c):>8}  routes={ru:>3}  status={st}")


# ─────────────────────────────────────────────────────────
# SUITE C — Short time budget cliff
# ─────────────────────────────────────────────────────────

def run_suite_C(cu: CuOptSolver, ort: ORToolsSolver, runs: list[RunResult]) -> None:
    print("\n" + "=" * 88)
    print(f"  SUITE C — Short time budget cliff (TL ∈ {SUITEC_TIME_LIMITS}s, "
          f"{len(SUITEC_SEEDS)} seeds por (TL, solver))")
    print("=" * 88)

    # Pre-generamos los 3 problemas (uno por seed) para reusarlos en todos los TLs.
    # Cada seed produce una instancia distinta del mismo size (1581 nodos).
    problems_by_seed: dict[int, VRPProblem] = {}
    for seed in SUITEC_SEEDS:
        circuits = _generate_circuits(SUITEC_GROUP_SIZE, seed)
        problems_by_seed[seed] = _build_consolidated(circuits)

    sample = next(iter(problems_by_seed.values()))
    n, v = sample.num_nodes, sample.num_vehicles
    print(f"\n## Problema: g={SUITEC_GROUP_SIZE} = {n} nodos, {v} trucks, "
          f"{len(SUITEC_SEEDS)} instancias (seeds={SUITEC_SEEDS})")

    for tl in SUITEC_TIME_LIMITS:
        print(f"\n-- TL={tl}s --")
        for seed in SUITEC_SEEDS:
            cons = problems_by_seed[seed]
            label = f"TL={tl}s,seed={seed}"

            # cuOpt self-hosted
            w, st, c, ru = _solve_once(cu, cons, tl)
            runs.append(RunResult(
                "C", "cuopt_consolidated_selfhosted", label,
                n, v, tl, w, c, st, ru,
            ))
            cost_str = f"{int(c):>8}" if c > 0 else f"{'INFEAS':>8}"
            print(f"  cuopt   seed={seed} wall={w:5.1f}s overhead={w-tl:+4.1f}s cost={cost_str} status={st}")

            # OR-Tools consolidated
            w, st, c, ru = _solve_once(ort, cons, tl)
            runs.append(RunResult(
                "C", "ortools_consolidated", label,
                n, v, tl, w, c, st, ru,
            ))
            cost_str = f"{int(c):>8}" if c > 0 else f"{'INFEAS':>8}"
            print(f"  ortools seed={seed} wall={w:5.1f}s overhead={w-tl:+4.1f}s cost={cost_str} status={st}")


# ─────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────

def report(runs: list[RunResult]) -> None:
    if any(r.suite == "0" for r in runs):
        print("\n" + "=" * 96)
        print("  SUITE 0 — Mega-consolidation comparativa")
        print("=" * 96)
        print(f"{'label':>10} {'nodes':>6} {'metric':>27} {'cuopt_sh':>11} {'ort_cons':>11} {'ort_seq':>11}")
        for lbl in sorted({r.label for r in runs if r.suite == "0"}):
            cu_c = next((r for r in runs if r.suite=="0" and r.label==lbl and r.mode=="cuopt_consolidated_selfhosted"), None)
            or_c = next((r for r in runs if r.suite=="0" and r.label==lbl and r.mode=="ortools_consolidated"), None)
            or_s = next((r for r in runs if r.suite=="0" and r.label==lbl and r.mode=="ortools_sequential"), None)
            if not (cu_c and or_c): continue
            n = cu_c.total_nodes
            seq_wall = f"{or_s.wall_clock_s:>10.1f}s" if or_s else f"{'—':>11}"
            seq_cost = f"{int(or_s.total_cost):>11}" if or_s else f"{'—':>11}"
            print(f"{lbl:>10} {n:>6} {'wall_clock_s':>27} {cu_c.wall_clock_s:>10.1f}s {or_c.wall_clock_s:>10.1f}s {seq_wall}")
            print(f"{lbl:>10} {n:>6} {'total_cost':>27} {int(cu_c.total_cost):>11} {int(or_c.total_cost):>11} {seq_cost}")
            if cu_c.total_cost > 0 and or_c.total_cost > 0:
                d = (or_c.total_cost - cu_c.total_cost) / cu_c.total_cost * 100
                print(f"{lbl:>10} {n:>6} {'Δ% cost vs cuopt_sh':>27} {'—':>11} {d:>+10.2f}% {'':>11}")
            if or_s and or_s.total_cost > 0 and cu_c.total_cost > 0:
                d = (or_s.total_cost - cu_c.total_cost) / cu_c.total_cost * 100
                sp = or_s.wall_clock_s / cu_c.wall_clock_s if cu_c.wall_clock_s > 0 else 0
                print(f"{lbl:>10} {n:>6} {'Δ% cost vs seq':>27} {'—':>11} {'':>11} {d:>+10.2f}%")
                print(f"{lbl:>10} {n:>6} {'wall speedup vs seq':>27} {sp:>10.2f}x {'':>11} {'':>11}")
            print()

    if any(r.suite == "A" for r in runs):
        print("\n" + "=" * 96)
        print(f"  SUITE A — Long time budget (g={SUITEA_GROUP_SIZE} = {next(r.total_nodes for r in runs if r.suite=='A')} nodos)")
        print("=" * 96)
        print(f"{'TL(s)':>6} {'cuopt cost':>12} {'ort cost':>12} {'Δ% OR vs cu':>13} {'cu wall':>10} {'ort wall':>10}")
        for tl in SUITEA_TIME_LIMITS:
            cu = next((r for r in runs if r.suite=="A" and r.time_limit_s==tl and r.mode=="cuopt_consolidated_selfhosted"), None)
            ot = next((r for r in runs if r.suite=="A" and r.time_limit_s==tl and r.mode=="ortools_consolidated"), None)
            if not (cu and ot): continue
            d = (ot.total_cost - cu.total_cost) / cu.total_cost * 100 if cu.total_cost > 0 else 0.0
            print(f"{tl:>6} {int(cu.total_cost):>12} {int(ot.total_cost):>12} {d:>+12.2f}% {cu.wall_clock_s:>9.1f}s {ot.wall_clock_s:>9.1f}s")

    if any(r.suite == "B" for r in runs):
        print("\n" + "=" * 96)
        print(f"  SUITE B — Time windows (CVRPTW)")
        print("=" * 96)
        cu = next((r for r in runs if r.suite=="B" and r.mode=="cuopt_consolidated_TW"), None)
        ot = next((r for r in runs if r.suite=="B" and r.mode=="ortools_consolidated_TW"), None)
        if cu and ot:
            print(f"{'mode':>30} {'cost':>12} {'wall(s)':>10} {'routes':>7} {'status':>15}")
            print(f"{'cuopt_selfhosted_TW':>30} {int(cu.total_cost):>12} {cu.wall_clock_s:>9.1f}s {cu.routes_used:>7} {cu.status:>15}")
            print(f"{'ortools_consolidated_TW':>30} {int(ot.total_cost):>12} {ot.wall_clock_s:>9.1f}s {ot.routes_used:>7} {ot.status:>15}")
            if cu.total_cost > 0 and ot.total_cost > 0:
                d = (ot.total_cost - cu.total_cost) / cu.total_cost * 100
                print(f"\nΔ% OR-Tools vs cuOpt (CVRPTW): {d:+.2f}%")

    if any(r.suite == "C" for r in runs):
        print("\n" + "=" * 110)
        print(f"  SUITE C — Short time budget cliff "
              f"(g={SUITEC_GROUP_SIZE}={next(r.total_nodes for r in runs if r.suite=='C')} nodos, "
              f"{len(SUITEC_SEEDS)} seeds)")
        print("=" * 110)
        print(f"{'TL(s)':>5} {'solver':>10} {'feasible':>10} "
              f"{'cost mean':>11} {'cost std':>10} {'cost min':>11} {'cost max':>11} "
              f"{'wall mean':>11} {'overhead':>10}")
        print("-" * 110)
        for tl in SUITEC_TIME_LIMITS:
            for mode, label in [
                ("cuopt_consolidated_selfhosted", "cuopt_sh"),
                ("ortools_consolidated", "ortools"),
            ]:
                rs = [r for r in runs if r.suite=="C" and r.time_limit_s==tl and r.mode==mode]
                if not rs:
                    continue
                feas = [r for r in rs if r.total_cost > 0]
                if feas:
                    costs = [r.total_cost for r in feas]
                    cmean = statistics.mean(costs)
                    cstd  = statistics.stdev(costs) if len(costs) > 1 else 0.0
                    cmin  = min(costs)
                    cmax  = max(costs)
                else:
                    cmean = cstd = cmin = cmax = 0.0
                walls = [r.wall_clock_s for r in rs]
                wmean = statistics.mean(walls)
                overhead = wmean - tl
                feas_str = f"{len(feas)}/{len(rs)}"
                print(f"{tl:>5} {label:>10} {feas_str:>10} "
                      f"{int(cmean) if cmean>0 else '—':>11} {int(cstd) if cstd>0 else '—':>10} "
                      f"{int(cmin) if cmin>0 else '—':>11} {int(cmax) if cmax>0 else '—':>11} "
                      f"{wmean:>10.1f}s {overhead:>+9.1f}s")
            # Δ% mean comparison
            cu_feas = [r.total_cost for r in runs if r.suite=="C" and r.time_limit_s==tl
                       and r.mode=="cuopt_consolidated_selfhosted" and r.total_cost > 0]
            or_feas = [r.total_cost for r in runs if r.suite=="C" and r.time_limit_s==tl
                       and r.mode=="ortools_consolidated" and r.total_cost > 0]
            if cu_feas and or_feas:
                cu_mean = statistics.mean(cu_feas)
                or_mean = statistics.mean(or_feas)
                delta = (or_mean - cu_mean) / cu_mean * 100 if cu_mean > 0 else 0.0
                print(f"{tl:>5} {'(Δ%)':>10} {'':>10} {'':>11} {'':>10} {'':>11} {'':>11} "
                      f"OR-T vs cuOpt: {delta:+.2f}%")
            print()


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=None,
                        help="URL of self-hosted cuOpt (default: read from results/.ec2_state.json)")
    parser.add_argument("--skip-or-sequential", action="store_true",
                        help="Skip OR-Tools sequential mode in SUITE 0")
    parser.add_argument("--suite", choices=["0", "A", "B", "C", "all"], default="all",
                        help="Which test suite to run (default: all)")
    args = parser.parse_args()

    server_url = _resolve_server(args.server)
    print(f"cuOpt self-hosted target: {server_url}")
    print(f"Suite(s) to run: {args.suite}")

    cu  = CuOptSolver(mode="self_hosted", server_url=server_url)
    ort = ORToolsSolver()

    runs: list[RunResult] = []

    if args.suite in ("0", "all"):
        run_suite_0(cu, ort, runs, args.skip_or_sequential)
    if args.suite in ("A", "all"):
        run_suite_A(cu, ort, runs)
    if args.suite in ("B", "all"):
        run_suite_B(cu, ort, runs)
    if args.suite in ("C", "all"):
        run_suite_C(cu, ort, runs)

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"supermega_extended_{ts}.json"
    with open(out_path, "w") as f:
        json.dump([asdict(r) for r in runs], f, indent=2)
    print(f"\nRaw JSON: {out_path}")

    report(runs)


if __name__ == "__main__":
    main()
