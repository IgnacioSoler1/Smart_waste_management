"""
benchmark_convergence.py — SmartWaste MVD

Measures TIME-TO-QUALITY for cuOpt vs OR-Tools on the same problem
instances. The headline metric is not the cost achieved at a fixed
time budget (already covered by benchmark_consolidation_*.py) but
the inverse: how long does each solver need to reach the SAME cost?

Two complementary techniques:

  - OR-Tools  → routing.AddAtSolutionCallback() logs (elapsed_s, cost)
                on every improvement during a single 60-second solve,
                giving a continuous convergence trajectory.
  - cuOpt     → multiple solves at time_limit ∈ {2, 3, 5, 10, 30, 60}s
                (cuOpt does not expose intermediate solutions via its
                REST API, so we sample the cost-vs-budget curve at
                discrete points).

The script then computes, per problem size and per quality target
(best cost achieved × (1 + tolerance%)), how much wall-clock time each
solver needed to reach it. The ratio is the "wall-clock speedup of
cuOpt to reach equal quality".

All cuOpt calls go to the cloud API Catalog (free tier 5,000 req/mo),
so no EC2 GPU is required. Problem sizes are capped at 949 nodes so the
whole experiment fits inside the cloud limit (~1,000 nodes per solve).

Usage:
  export CUOPT_API_KEY=<nvapi-key>
  python3 -u benchmark_convergence.py
  python3 -u benchmark_convergence.py --skip-large
  python3 -u benchmark_convergence.py --ortools-only        # no cuOpt calls
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vrp_solver import CuOptSolver  # type: ignore
from problem_generator import ProblemGenerator, VRPProblem  # type: ignore

logging.basicConfig(level=logging.WARNING)


# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────

CIRCUIT_SIZE       = 80    # containers per synthetic circuit (depot adds 1)
TRUCKS_PER_CIRCUIT = 2

# (label, target_node_count, target_vehicle_count)
# All sizes stay within the cuOpt cloud API limit (~1000 nodes).
PROBLEM_CONFIGS = [
    ("small",  80,  1),    # 1 circuit, 1 truck  — Regime A (TSP-like)
    ("medium", 238, 6),    # 3 circuits, 6 trucks — Regime B (lower)
    ("large",  949, 24),   # 12 circuits, 24 trucks — Regime B (upper, cloud cap)
]

CUOPT_TL_GRID    = [2, 3, 5, 10, 30, 60]   # 6 sample points for cuOpt
ORTOOLS_BUDGET   = 60                       # single OR-Tools run, log trajectory
N_SEEDS          = 3
N_SEEDS_LARGE    = 2                        # large is heavier, fewer seeds

OUT_DIR = Path("results")
OUT_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────

@dataclass
class CuOptRun:
    size_label: str
    total_nodes: int
    num_vehicles: int
    seed: int
    time_limit_s: int
    wall_clock_s: float
    status: str
    cost: float


@dataclass
class TrajectoryPoint:
    elapsed_s: float
    cost: float


@dataclass
class ORToolsRun:
    size_label: str
    total_nodes: int
    num_vehicles: int
    seed: int
    time_budget_s: int
    wall_clock_s: float
    final_status: str
    final_cost: float
    trajectory: list = field(default_factory=list)


# ─────────────────────────────────────────────────────────
# Problem generation (matches benchmark_consolidation_*.py)
# ─────────────────────────────────────────────────────────

def _generate_problem(label: str, total_nodes: int, num_vehicles: int, seed: int) -> VRPProblem:
    """
    Build a VRPProblem of the requested label/size.
      - "small": single ProblemGenerator.generate() call (TSP-like).
      - "medium"/"large": consolidate N synthetic circuits of 80 containers
        each, with shared depot and pooled fleet — same recipe used by
        benchmark_consolidation_cloud.py.
    """
    gen = ProblemGenerator()

    if label == "small":
        return gen.generate(num_nodes=total_nodes, num_vehicles=num_vehicles, seed=seed)

    num_circuits = num_vehicles // TRUCKS_PER_CIRCUIT
    circuits = [
        gen.generate(num_nodes=CIRCUIT_SIZE, num_vehicles=TRUCKS_PER_CIRCUIT, seed=seed + i)
        for i in range(num_circuits)
    ]

    coords = [circuits[0].coordinates[0]]   # shared depot
    demands = [0.0]
    capacities: list[float] = []
    for c in circuits:
        coords.extend(c.coordinates[1:])
        demands.extend(c.demands[1:])
        capacities.extend(c.capacities)

    cost_matrix = gen._build_cost_matrix(coords)
    return VRPProblem(
        name=f"{label}_seed{seed}",
        num_nodes=len(coords),
        num_vehicles=len(capacities),
        cost_matrix=cost_matrix,
        demands=demands,
        capacities=capacities,
        depot_start_idx=0,
        depot_end_idx=0,
        coordinates=coords,
    )


# ─────────────────────────────────────────────────────────
# OR-Tools with AtSolutionCallback (continuous trajectory)
# ─────────────────────────────────────────────────────────

def _solve_ortools_with_trajectory(problem: VRPProblem, time_budget: int) -> tuple[float, str, float, list[TrajectoryPoint]]:
    """
    Run OR-Tools with AddAtSolutionCallback; collect (elapsed_s, cost) for
    every improvement. Returns (wall_clock_s, status, final_cost, trajectory).
    """
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    n          = problem.num_nodes
    cost_mat   = problem.cost_matrix
    cap_int    = [int(c) for c in problem.capacities]
    dem_int    = [int(d) for d in problem.demands]

    manager = pywrapcp.RoutingIndexManager(n, problem.num_vehicles, problem.depot_start_idx)
    routing = pywrapcp.RoutingModel(manager)

    def cost_callback(from_idx: int, to_idx: int) -> int:
        return cost_mat[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    transit_cb = routing.RegisterTransitCallback(cost_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    def demand_callback(idx: int) -> int:
        return dem_int[manager.IndexToNode(idx)]

    demand_cb = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(demand_cb, 0, cap_int, True, "Capacity")

    trajectory: list[TrajectoryPoint] = []
    start = time.perf_counter()

    def at_solution() -> None:
        elapsed = time.perf_counter() - start
        cost    = routing.CostVar().Value()
        trajectory.append(TrajectoryPoint(elapsed_s=elapsed, cost=float(cost)))

    routing.AddAtSolutionCallback(at_solution)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = time_budget
    params.log_search = False

    solution = routing.SolveWithParameters(params)
    wall = time.perf_counter() - start

    if solution is None:
        return wall, "NO_SOLUTION", 0.0, trajectory

    status_map = {7: "OPTIMAL", 1: "FEASIBLE", 2: "FEASIBLE", 4: "FEASIBLE",
                  3: "NO_SOLUTION", 6: "INFEASIBLE"}
    status = status_map.get(routing.status(), "FEASIBLE")
    return wall, status, float(solution.ObjectiveValue()), trajectory


# ─────────────────────────────────────────────────────────
# cuOpt at single TL
# ─────────────────────────────────────────────────────────

def _solve_cuopt(solver: CuOptSolver, problem: VRPProblem, tl: int) -> tuple[float, str, float]:
    start = time.perf_counter()
    try:
        r = solver.solve_vrp(
            cost_matrix     = problem.cost_matrix,
            num_vehicles    = problem.num_vehicles,
            demands         = problem.demands,
            capacities      = problem.capacities,
            depot_start_idx = problem.depot_start_idx,
            depot_end_idx   = problem.depot_end_idx,
            time_limit      = tl,
        )
        return time.perf_counter() - start, r.get("status", "UNK"), float(r.get("total_cost", 0))
    except Exception as e:
        return time.perf_counter() - start, f"ERR:{str(e)[:60]}", 0.0


# ─────────────────────────────────────────────────────────
# Time-to-target analysis
# ─────────────────────────────────────────────────────────

def _wall_to_target_cuopt(seed_runs: list[CuOptRun], target_cost: float) -> float | None:
    """Smallest cuOpt wall_clock_s in this seed at which cost ≤ target."""
    feas = [r for r in seed_runs if r.cost > 0]
    feas.sort(key=lambda r: r.time_limit_s)
    for r in feas:
        if r.cost <= target_cost:
            return r.wall_clock_s
    return None


def _wall_to_target_ortools(trajectory: list[TrajectoryPoint], target_cost: float) -> float | None:
    """Earliest trajectory elapsed_s at which cost ≤ target."""
    for p in trajectory:
        if p.cost <= target_cost:
            return p.elapsed_s
    return None


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-large",   action="store_true",
                        help="Skip the 949-node problem (saves ~8 min)")
    parser.add_argument("--ortools-only", action="store_true",
                        help="Run only OR-Tools (no cuOpt API calls)")
    parser.add_argument("--no-png",       action="store_true",
                        help="Skip matplotlib PNG generation")
    args = parser.parse_args()

    if not args.ortools_only and not os.environ.get("CUOPT_API_KEY"):
        print("⚠ CUOPT_API_KEY not set. Use --ortools-only or export the key.")
        sys.exit(1)

    cu = CuOptSolver(mode="api_catalog") if not args.ortools_only else None

    configs = PROBLEM_CONFIGS
    if args.skip_large:
        configs = [c for c in configs if c[0] != "large"]

    cuopt_runs: list[CuOptRun]   = []
    ortools_runs: list[ORToolsRun] = []

    print(f"benchmark_convergence — configs={[c[0] for c in configs]}, "
          f"cuOpt TL grid={CUOPT_TL_GRID}, OR-Tools budget={ORTOOLS_BUDGET}s\n")

    for label, total_nodes, num_vehicles in configs:
        n_seeds = N_SEEDS_LARGE if label == "large" else N_SEEDS
        print(f"\n## {label} — target {total_nodes} nodes / {num_vehicles} vehicles / {n_seeds} seeds")

        for seed_idx in range(n_seeds):
            seed = 7000 + seed_idx * 100
            problem = _generate_problem(label, total_nodes, num_vehicles, seed)
            print(f"\n  seed={seed}  →  {problem.num_nodes} nodes, {problem.num_vehicles} vehicles")

            # OR-Tools with trajectory
            print(f"    OR-Tools (budget={ORTOOLS_BUDGET}s, AddAtSolutionCallback)...")
            wall, st, fc, traj = _solve_ortools_with_trajectory(problem, ORTOOLS_BUDGET)
            print(f"      → improvements logged: {len(traj):>3}  |  final cost={int(fc):>8}  "
                  f"|  wall={wall:5.1f}s  |  status={st}")
            ortools_runs.append(ORToolsRun(
                size_label    = label,
                total_nodes   = problem.num_nodes,
                num_vehicles  = problem.num_vehicles,
                seed          = seed,
                time_budget_s = ORTOOLS_BUDGET,
                wall_clock_s  = wall,
                final_status  = st,
                final_cost    = fc,
                trajectory    = traj,
            ))

            # cuOpt at multiple TLs
            if cu is not None:
                for tl in CUOPT_TL_GRID:
                    wall, st, cost = _solve_cuopt(cu, problem, tl)
                    cost_str = f"{int(cost):>8}" if cost > 0 else f"{'INFEAS':>8}"
                    print(f"    cuOpt TL={tl:>3}s  →  cost={cost_str}  |  wall={wall:5.1f}s  |  status={st}")
                    cuopt_runs.append(CuOptRun(
                        size_label    = label,
                        total_nodes   = problem.num_nodes,
                        num_vehicles  = problem.num_vehicles,
                        seed          = seed,
                        time_limit_s  = tl,
                        wall_clock_s  = wall,
                        status        = st,
                        cost          = cost,
                    ))

    # ── Persist raw ────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"convergence_{ts}.json"
    with open(out_path, "w") as f:
        json.dump({
            "cuopt_runs":   [asdict(r) for r in cuopt_runs],
            "ortools_runs": [
                {**asdict(r), "trajectory": [asdict(p) for p in r.trajectory]}
                for r in ortools_runs
            ],
        }, f, indent=2)
    print(f"\nRaw JSON: {out_path}")

    # ── Analysis: time-to-target ──────────────────────────
    print("\n" + "=" * 102)
    print("  CONVERGENCE — wall-clock to reach a target cost (median across seeds)")
    print("=" * 102)

    for label, _, _ in configs:
        sub_cu = [r for r in cuopt_runs   if r.size_label == label]
        sub_or = [r for r in ortools_runs if r.size_label == label]
        if not sub_or:
            continue

        # Best cost observed across both solvers + all seeds
        all_costs = [r.cost for r in sub_cu if r.cost > 0] + \
                    [r.final_cost for r in sub_or if r.final_cost > 0]
        if not all_costs:
            print(f"\n## {label}: no feasible runs, skipping")
            continue

        best  = min(all_costs)
        seeds_cu = sorted({r.seed for r in sub_cu})
        seeds_or = sorted({r.seed for r in sub_or})

        print(f"\n## {label} (n_nodes={sub_or[0].total_nodes}, "
              f"best cost observed = {int(best)})")
        print(f"   {'target':>30} | {'cuOpt wall (median)':>26} | "
              f"{'OR-Tools wall (median)':>26} | {'speedup':>8}")
        print(f"   {'-'*30} | {'-'*26} | {'-'*26} | {'-'*8}")

        for tol_pct in [0, 1, 3, 5, 10]:
            target = best * (1 + tol_pct / 100)

            cu_walls: list[float] = []
            for s in seeds_cu:
                seed_runs = [r for r in sub_cu if r.seed == s]
                t = _wall_to_target_cuopt(seed_runs, target)
                if t is not None:
                    cu_walls.append(t)

            or_walls: list[float] = []
            for r in sub_or:
                t = _wall_to_target_ortools(r.trajectory, target)
                if t is not None:
                    or_walls.append(t)

            cu_med = statistics.median(cu_walls) if cu_walls else None
            or_med = statistics.median(or_walls) if or_walls else None

            target_lbl = f"best × (1+{tol_pct}%) = {int(target)}"
            cu_str = (f"{cu_med:>5.1f}s  ({len(cu_walls)}/{len(seeds_cu)} seeds)"
                      if cu_med is not None else f"never  ({0}/{len(seeds_cu)})")
            or_str = (f"{or_med:>5.1f}s  ({len(or_walls)}/{len(seeds_or)} seeds)"
                      if or_med is not None else f"never  ({0}/{len(seeds_or)})")
            sp_str = (f"{or_med / cu_med:>6.1f}×"
                      if (cu_med and or_med) else "—")

            print(f"   {target_lbl:>30} | {cu_str:>26} | {or_str:>26} | {sp_str:>8}")

    # ── PNG plot (optional) ───────────────────────────────
    if args.no_png:
        return
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib not installed — skipping PNG)")
        return

    n = len(configs)
    fig, axes = plt.subplots(1, n, figsize=(5 * n, 4.2), squeeze=False)
    for ax, (label, _, _) in zip(axes[0], configs):
        sub_cu = [r for r in cuopt_runs   if r.size_label == label and r.cost > 0]
        sub_or = [r for r in ortools_runs if r.size_label == label]

        # OR-Tools trajectories (per seed)
        for i, r in enumerate(sub_or):
            if not r.trajectory:
                continue
            xs = [p.elapsed_s for p in r.trajectory]
            ys = [p.cost for p in r.trajectory]
            ax.plot(xs, ys, color="tab:orange", alpha=0.55, linewidth=1.4,
                    label="OR-Tools (live)" if i == 0 else None,
                    drawstyle="steps-post")

        # cuOpt sample points (per seed)
        for i, s in enumerate(sorted({r.seed for r in sub_cu})):
            seed_runs = sorted([r for r in sub_cu if r.seed == s], key=lambda r: r.wall_clock_s)
            xs = [r.wall_clock_s for r in seed_runs]
            ys = [r.cost for r in seed_runs]
            ax.scatter(xs, ys, c="tab:blue", s=42, alpha=0.85,
                       marker="o", edgecolors="navy", linewidths=0.5,
                       label="cuOpt (cost @ wall-clock)" if i == 0 else None)

        nodes = sub_or[0].total_nodes if sub_or else "?"
        ax.set_title(f"{label} — {nodes} nodes")
        ax.set_xlabel("Wall-clock (s, log scale)")
        ax.set_ylabel("Cost")
        ax.set_xscale("log")
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.3, which="both")

    plt.tight_layout()
    png_path = OUT_DIR / f"convergence_{ts}.png"
    plt.savefig(png_path, dpi=150)
    print(f"PNG:      {png_path}")


if __name__ == "__main__":
    main()
