"""
benchmark_megascale.py — SmartWaste MVD

Test: 1000 nodos (límite cloud API), multi-vehicle, capacidad tight.
Régimen Gehring & Homberger donde cuOpt tiene sus world records.
Genera los números de §2 Regime C en docs/cuopt.md.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vrp_solver import CuOptSolver, ORToolsSolver  # type: ignore
from problem_generator import ProblemGenerator, VRPProblem  # type: ignore

logging.basicConfig(level=logging.WARNING)


def override_capacity(p: VRPProblem, slack: float) -> VRPProblem:
    cap = (sum(p.demands) * slack) / p.num_vehicles
    p.capacities = [cap] * p.num_vehicles
    return p


def solve(solver, p: VRPProblem, tl: int):
    start = time.perf_counter()
    try:
        r = solver.solve_vrp(
            cost_matrix=p.cost_matrix, num_vehicles=p.num_vehicles,
            demands=p.demands, capacities=p.capacities,
            depot_start_idx=0, depot_end_idx=0, time_limit=tl,
        )
        return (
            (time.perf_counter() - start) * 1000,
            r.get("status", "UNK"),
            float(r.get("total_cost", 0)),
        )
    except Exception as e:
        return ((time.perf_counter() - start) * 1000, f"ERR:{str(e)[:50]}", 0.0)


def main():
    SIZES    = [800, 1000]
    VEHICLES = [10, 20]
    SLACKS   = [1.05]
    TL       = 10
    SEED     = 42

    gen = ProblemGenerator()
    cu  = CuOptSolver(mode="api_catalog")
    ort = ORToolsSolver()

    runs = []
    print(f"{'size':>5} {'veh':>4} {'slack':>6} {'solver':>9} {'status':>12} {'cost':>10} {'lat(s)':>8}")
    print("-" * 75)

    for s in SIZES:
        for v in VEHICLES:
            for sl in SLACKS:
                p = override_capacity(gen.generate(s, v, SEED), sl)

                lat, st, c = solve(ort, p, TL)
                runs.append({"size": s, "vehicles": v, "slack": sl, "solver": "ortools",
                             "latency_ms": lat, "status": st, "cost": c})
                print(f"{s:>5} {v:>4} {sl:>6.2f} {'or-tools':>9} {st[:12]:>12} {int(c):>10} {lat/1000:>7.1f}s")

                lat, st, c = solve(cu, p, TL)
                runs.append({"size": s, "vehicles": v, "slack": sl, "solver": "cuopt",
                             "latency_ms": lat, "status": st, "cost": c})
                print(f"{s:>5} {v:>4} {sl:>6.2f} {'cuopt':>9} {st[:12]:>12} {int(c):>10} {lat/1000:>7.1f}s")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    Path("results").mkdir(exist_ok=True)
    out = Path("results") / f"megascale_{ts}.json"
    with open(out, "w") as f:
        json.dump(runs, f, indent=2)
    print(f"\nRaw: {out}")

    # Comparativa
    print("\n" + "=" * 70)
    print("  Δ% = (OR-Tools cost − cuOpt cost) / cuOpt cost × 100  [+ve = cuOpt mejor]")
    print("=" * 70)
    print(f"{'size':>5} {'veh':>4} {'slack':>6} {'cu_cost':>10} {'or_cost':>10} {'Δ%':>8}")
    for s in SIZES:
        for v in VEHICLES:
            for sl in SLACKS:
                cu_r = next((r for r in runs if r["size"]==s and r["vehicles"]==v and r["slack"]==sl and r["solver"]=="cuopt"), None)
                or_r = next((r for r in runs if r["size"]==s and r["vehicles"]==v and r["slack"]==sl and r["solver"]=="ortools"), None)
                if cu_r and or_r and cu_r["cost"] > 0 and or_r["cost"] > 0:
                    delta = (or_r["cost"] - cu_r["cost"]) / cu_r["cost"] * 100
                    print(f"{s:>5} {v:>4} {sl:>6.2f} {int(cu_r['cost']):>10} {int(or_r['cost']):>10} {delta:>+7.2f}%")
                else:
                    cu_st = cu_r["status"][:8] if cu_r else "—"
                    or_st = or_r["status"][:8] if or_r else "—"
                    print(f"{s:>5} {v:>4} {sl:>6.2f} cu:{cu_st:>8} or:{or_st:>8}")


if __name__ == "__main__":
    main()
