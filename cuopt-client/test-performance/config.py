"""
config.py — SmartWaste MVD Performance Tests

Constantes centralizadas para la suite de benchmarks de cuOpt.
"""

# ── Dimensiones del test ─────────────────────────────────

# Tamaños de problema (nodos incluyendo depot)
PROBLEM_SIZES = [50, 100, 150, 200]

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

# Tiempo de cómputo del solver (segundos).
# NOTA: vrp_solver.py usa time_limit * 10_000 como ventana de tiempo
# del vehículo (100,000s = 27.7 horas). La cost_matrix se genera en
# segundos (no metros) para que las magnitudes sean coherentes.
# Con time_limit=10, todos los escenarios son feasible excepto 200n/1v
# (48h de ruta > 27.7h ventana), lo cual es realista.
SOLVER_TIME_LIMIT = 10

# Velocidad promedio urbana para convertir metros → segundos en cost_matrix.
# 30 km/h = 8.33 m/s (misma asunción que osrm_client.py haversine fallback).
URBAN_SPEED_MS = 8.33

# Depot: Felipe Cardoso (noreste de Montevideo)
DEPOT_LAT = -34.8347
DEPOT_LON = -56.0967

# ── Rate limits — API Catalog ────────────────────────────

API_CATALOG_DELAY_BETWEEN_REQUESTS = 1.0   # segundos entre requests secuenciales
API_CATALOG_MAX_CONCURRENT = 5             # máx requests simultáneos
API_CATALOG_WARN_THRESHOLD = 200           # advertir si el plan excede este número
API_CATALOG_SAFETY_LIMIT = 500             # abort si se llega a este total en un run

# ── Ramp-up ──────────────────────────────────────────────

# Ramp-up más conservador (1 GPU no aguanta más de ~5 concurrent)
RAMP_STEPS = [1, 2, 5]
RAMP_STEP_DURATION_SECS = 30

# ── Quick mode (subconjunto reducido) ────────────────────

QUICK_PROBLEM_SIZES = [50, 100, 150, 200]
QUICK_CONCURRENCY_LEVELS = [1, 5]
QUICK_VEHICLE_COUNTS = [1]
QUICK_REPETITIONS = 1

# ── Stress de alta concurrencia (solo en quick mode) ─────
# Se corre al final, únicamente sobre el problema más pequeño
# para medir cuánto se degrada la latencia bajo carga alta.
# bypass_limit=True en run_concurrent para superar API_CATALOG_MAX_CONCURRENT.
QUICK_STRESS_CONCURRENCY_LEVELS = [10, 20, 30]
QUICK_STRESS_PROBLEM_SIZE = 50
QUICK_STRESS_VEHICLE_COUNT = 1

# ── Output ───────────────────────────────────────────────

RESULTS_DIR = "results"
