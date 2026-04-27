"""
test_solver.py — SmartWaste MVD

Test funcional del VRP solver con un problema sintético pequeño.

Problema:
  - 10 nodos: 1 depósito (índice 0) + 9 contenedores
  - 2 vehículos, capacidad 300 kg cada uno
  - Demandas varían entre 20 y 80 kg
  - Matriz de costos basada en distancias euclídeas de puntos en Montevideo

Uso:
  python cuopt-client/test_solver.py              # OR-Tools (default)
  python cuopt-client/test_solver.py --cuopt      # cuOpt API Catalog
  python cuopt-client/test_solver.py --cuopt --mode self_hosted --server http://localhost:8080
"""

from __future__ import annotations

import argparse
import logging
import math
import sys

from constraints import estimate_demand_kg, get_time_window
from vrp_solver import CuOptSolver, ORToolsSolver

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# Datos sintéticos del problema de prueba
# ─────────────────────────────────────────────────────────

# 10 nodos: índice 0 = depósito Felipe Cardoso, 1–9 = contenedores
# Coordenadas (lat, lon) de puntos reales en Montevideo
NODES: list[tuple[str, float, float]] = [
    ("Depósito Felipe Cardoso",  -34.8347, -56.0967),  # 0 — depósito
    ("Centro / 18 de Julio",     -34.9059, -56.1913),  # 1
    ("Pocitos",                  -34.9008, -56.1526),  # 2
    ("Cerro",                    -34.8924, -56.2476),  # 3
    ("Punta Carretas",           -34.9178, -56.1564),  # 4
    ("Carrasco",                 -34.8934, -56.0543),  # 5
    ("Malvín",                   -34.8946, -56.1061),  # 6
    ("Buceo",                    -34.9015, -56.1318),  # 7
    ("Prado",                    -34.8728, -56.1913),  # 8
    ("Tres Cruces",              -34.8978, -56.1684),  # 9
]

# Niveles de llenado simulados (%) para calcular demanda en kg
FILL_LEVELS = [0.0, 85.0, 60.0, 90.0, 45.0, 70.0, 55.0, 80.0, 40.0, 95.0]

# Capacidad del contenedor estándar de Montevideo (litros)
CONTAINER_CAPACITY_LITERS = 2400

# Configuración de la flota.
# Camiones reales tienen 25 t (25 000 kg), pero reducimos a 2 500 kg en el
# test para forzar que los 9 contenedores se repartan entre los 2 vehículos.
# Demanda total ≈ 4 464 kg → 2 × 2 500 = 5 000 kg alcanza para cubrirla.
NUM_VEHICLES = 2
VEHICLE_CAPACITY_KG = 2500.0
DEPOT_IDX = 0


# ─────────────────────────────────────────────────────────
# Construcción de la matriz de costos
# ─────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Distancia entre dos coordenadas WGS84 en metros (entero)."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return int(2 * R * math.asin(math.sqrt(a)))


def build_cost_matrix(nodes: list[tuple[str, float, float]]) -> list[list[int]]:
    """Construye matriz N×N de distancias haversine en metros."""
    n = len(nodes)
    return [
        [_haversine_m(nodes[i][1], nodes[i][2], nodes[j][1], nodes[j][2]) for j in range(n)]
        for i in range(n)
    ]


# ─────────────────────────────────────────────────────────
# Helpers de presentación
# ─────────────────────────────────────────────────────────

def print_matrix(matrix: list[list[int]], labels: list[str], title: str) -> None:
    print(f"\n{'='*60}")
    print(title)
    print("="*60)
    col_w = 8
    print(f"{'':22}", end="")
    for lbl in labels:
        print(f"{lbl[:col_w]:>{col_w}}", end="")
    print()
    print("-" * (22 + col_w * len(labels)))
    for i, lbl in enumerate(labels):
        print(f"{lbl[:22]:<22}", end="")
        for j in range(len(labels)):
            print(f"{matrix[i][j]:>{col_w}}", end="")
        print()


def print_solution(result: dict, nodes: list[tuple[str, float, float]], demands: list[float]) -> None:
    print(f"\n{'='*60}")
    print(f"Solución VRP  [solver={result['solver']}  status={result['status']}]")
    print("="*60)
    print(f"  Costo total : {result['total_cost']:,} m")

    routes = result["routes"]
    if not routes:
        print("  (sin rutas — problema infactible o sin solución)")
        return

    for v_id, route in sorted(routes.items()):
        load = sum(demands[n] for n in route if n != DEPOT_IDX)
        stops = " → ".join(nodes[n][0] for n in route)
        print(f"\n  Vehículo {v_id}  (carga={load:.0f} kg / {VEHICLE_CAPACITY_KG:.0f} kg)")
        print(f"    {stops}")


# ─────────────────────────────────────────────────────────
# Runner principal
# ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Test VRP solver — SmartWaste MVD")
    parser.add_argument("--cuopt", action="store_true", help="Usar NVIDIA cuOpt en lugar de OR-Tools")
    parser.add_argument("--mode", default="api_catalog", choices=["api_catalog", "self_hosted"],
                        help="Modo cuOpt (default: api_catalog)")
    parser.add_argument("--server", default=None,
                        help="URL servidor cuOpt self-hosted (ej: http://localhost:8080)")
    parser.add_argument("--time-limit", type=int, default=5, help="Segundos de cómputo (default: 5)")
    args = parser.parse_args()

    # ── Datos del problema ────────────────────────────────────
    labels  = [n[0].split("/")[0].strip()[:10] for n in NODES]
    demands = [
        estimate_demand_kg(fill, CONTAINER_CAPACITY_LITERS)
        for fill in FILL_LEVELS
    ]
    cost_matrix = build_cost_matrix(NODES)

    # ── Mostrar configuración ─────────────────────────────────
    print("SmartWaste MVD — Test VRP Solver")
    print(f"Nodos     : {len(NODES)} (1 depósito + {len(NODES)-1} contenedores)")
    print(f"Vehículos : {NUM_VEHICLES} × {VEHICLE_CAPACITY_KG:.0f} kg")
    print(f"Backend   : {'NVIDIA cuOpt (' + args.mode + ')' if args.cuopt else 'OR-Tools (CPU)'}")
    print(f"Time limit: {args.time_limit} s")

    print("\nDemandas por nodo:")
    for i, (node, fill, demand) in enumerate(zip(NODES, FILL_LEVELS, demands)):
        bar = "█" * int(fill / 10) + "░" * (10 - int(fill / 10))
        marker = " ← depósito" if i == DEPOT_IDX else ""
        print(f"  [{i:2}] {node[0]:<28}  fill={fill:5.1f}%  {bar}  {demand:5.1f} kg{marker}")

    # ── Verificar ventanas de tiempo (constraints.py) ─────────
    print("\nVentanas de tiempo por turno:")
    for shift in ("morning", "afternoon", "night"):
        start, end = get_time_window(shift)
        print(f"  {shift:<12}: {start//3600:02d}:00 – {end//3600:02d}:00")

    # ── Mostrar fragmento de la matriz ────────────────────────
    print_matrix(cost_matrix, labels, "Matriz de costos (metros, haversine) — primeros 5×5")
    # Solo imprimimos 5×5 para no saturar la salida
    sub = [row[:5] for row in cost_matrix[:5]]
    sub_labels = labels[:5]
    col_w = 8
    print(f"{'':22}", end="")
    for lbl in sub_labels:
        print(f"{lbl:>{col_w}}", end="")
    print()
    print("-" * (22 + col_w * 5))
    for i, lbl in enumerate(sub_labels):
        print(f"{lbl:<22}", end="")
        for j in range(5):
            print(f"{sub[i][j]:>{col_w}}", end="")
        print()

    # ── Construir solver ──────────────────────────────────────
    if args.cuopt:
        try:
            solver = CuOptSolver(mode=args.mode, server_url=args.server)
        except ValueError as exc:
            print(f"\nERROR al crear CuOptSolver: {exc}")
            sys.exit(1)
    else:
        solver = ORToolsSolver()

    # ── Resolver ──────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("Resolviendo VRP...")

    try:
        result = solver.solve_vrp(
            cost_matrix     = cost_matrix,
            num_vehicles    = NUM_VEHICLES,
            demands         = demands,
            capacities      = [VEHICLE_CAPACITY_KG] * NUM_VEHICLES,
            depot_start_idx = DEPOT_IDX,
            depot_end_idx   = DEPOT_IDX,
            time_limit      = args.time_limit,
        )
    except ImportError as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"\nERROR al resolver: {exc}")
        logger.exception("Excepción en solve_vrp")
        sys.exit(1)

    print_solution(result, NODES, demands)

    # ── Validaciones básicas ──────────────────────────────────
    print(f"\n{'─'*60}")
    print("Validaciones:")

    ok = True

    def check(cond: bool, msg: str) -> bool:
        mark = "\033[92m✓\033[0m" if cond else "\033[91m✗\033[0m"
        print(f"  {mark} {msg}")
        return cond

    ok &= check(result["status"] in ("OPTIMAL", "FEASIBLE"),
                f"Status es solucionable: {result['status']}")
    ok &= check(len(result["routes"]) >= 1,
                f"Al menos 1 ruta activa ({len(result['routes'])} encontradas)")
    ok &= check(result["total_cost"] > 0,
                f"Costo total > 0 ({result['total_cost']:,})")

    for v_id, route in result["routes"].items():
        load = sum(demands[n] for n in route if n != DEPOT_IDX)
        ok &= check(load <= VEHICLE_CAPACITY_KG,
                    f"Vehículo {v_id}: carga {load:.0f} kg ≤ {VEHICLE_CAPACITY_KG:.0f} kg")
        ok &= check(route[0] == DEPOT_IDX and route[-1] == DEPOT_IDX,
                    f"Vehículo {v_id}: ruta empieza y termina en depósito")

    print()
    if ok:
        print("\033[92mTodos los checks pasaron ✓\033[0m")
        sys.exit(0)
    else:
        print("\033[91mAlgunos checks fallaron ✗\033[0m")
        sys.exit(1)


if __name__ == "__main__":
    main()
