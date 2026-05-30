"""
config.py — SmartWaste MVD Performance Tests

Constantes centralizadas para la suite de benchmarks de cuOpt.
"""

# ── Dimensiones del test ─────────────────────────────────

# Tamaños de problema (nodos incluyendo depot)
PROBLEM_SIZES = [10, 50, 100, 200]

# Niveles de concurrencia (requests simultáneos)
CONCURRENCY_LEVELS = [1, 5, 10]

# Cantidad de vehículos a testear
VEHICLE_COUNTS = [1, 2, 3]

# Repeticiones por caso de test
DEFAULT_REPETITIONS = 3

# ── Parámetros del problema VRP ──────────────────────────

# Capacidad por vehículo (kg) — reducida vs. real (25000) para forzar multi-ruta
VEHICLE_CAPACITY_KG = 5000.0

# Capacidad del contenedor estándar (litros)
CONTAINER_CAPACITY_LITERS = 2400

# Tiempo de cómputo del solver (segundos)
SOLVER_TIME_LIMIT = 5

# Depot: Felipe Cardoso (noreste de Montevideo)
DEPOT_LAT = -34.8347
DEPOT_LON = -56.0967

# ── Rate limits — API Catalog ────────────────────────────

API_CATALOG_DELAY_BETWEEN_REQUESTS = 1.0   # segundos entre requests secuenciales
API_CATALOG_MAX_CONCURRENT = 5             # máx requests simultáneos
API_CATALOG_WARN_THRESHOLD = 200           # advertir si el plan excede este número
API_CATALOG_SAFETY_LIMIT = 500             # abort si se llega a este total en un run

# ── Ramp-up ──────────────────────────────────────────────

RAMP_STEPS = [1, 2, 5, 10, 15, 20]
RAMP_STEP_DURATION_SECS = 30

# ── Quick mode (subconjunto reducido) ────────────────────

QUICK_PROBLEM_SIZES = [10, 50, 100]
QUICK_CONCURRENCY_LEVELS = [1, 5]
QUICK_VEHICLE_COUNTS = [2]
QUICK_REPETITIONS = 1

# ── Output ───────────────────────────────────────────────

RESULTS_DIR = "results"
