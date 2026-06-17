#!/usr/bin/env python3
"""
run_benchmarks.py — SmartWaste MVD Performance Tests

CLI principal para ejecutar benchmarks de performance de cuOpt.

Uso:
  # Self-hosted
  python run_benchmarks.py --mode self_hosted --server http://<IP>:5000

  # API Catalog
  python run_benchmarks.py --mode api_catalog

  # Ambos modos
  python run_benchmarks.py --mode both --server http://<IP>:5000

  # Quick mode (subset reducido)
  python run_benchmarks.py --mode self_hosted --server http://localhost:5000 --quick

  # Custom
  python run_benchmarks.py --mode self_hosted --server http://localhost:5000 \\
    --sizes 10,50,100 --concurrency 1,5,10 --vehicles 2,4
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from benchmark_runner import BenchmarkRunner
from config import (
    API_CATALOG_WARN_THRESHOLD,
    CONCURRENCY_LEVELS,
    DEFAULT_REPETITIONS,
    PROBLEM_SIZES,
    QUICK_CONCURRENCY_LEVELS,
    QUICK_PROBLEM_SIZES,
    QUICK_REPETITIONS,
    QUICK_STRESS_CONCURRENCY_LEVELS,
    QUICK_STRESS_PROBLEM_SIZE,
    QUICK_STRESS_VEHICLE_COUNT,
    QUICK_VEHICLE_COUNTS,
    RAMP_STEP_DURATION_SECS,
    RAMP_STEPS,
    RESULTS_DIR,
    VEHICLE_COUNTS,
)
from problem_generator import ProblemGenerator
from result_collector import RequestResult, ResultCollector
from visualizer import Visualizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _parse_int_list(value: str) -> list[int]:
    """Parsea '10,50,100' a [10, 50, 100]."""
    return [int(x.strip()) for x in value.split(",")]


def _estimate_requests(
    sizes: list[int],
    vehicles: list[int],
    concurrency: list[int],
    repetitions: int,
    modes: list[str],
    skip_ramp: bool,
) -> dict[str, int]:
    """Estima el total de requests por modo."""
    estimates: dict[str, int] = {}

    for mode in modes:
        # Sequential: cada (size, vehicle) combo × repetitions
        seq = len(sizes) * len(vehicles) * repetitions

        # Concurrent: cada (size, vehicles) × cada nivel >1 × repetitions
        conc_levels = [c for c in concurrency if c > 1]
        if mode == "api_catalog":
            conc_levels = [min(c, 5) for c in conc_levels]
        conc = len(sizes) * len(vehicles) * sum(conc_levels) * repetitions

        # Ramp-up: solo self_hosted
        ramp = 0
        if mode == "self_hosted" and not skip_ramp:
            # Estimación conservadora: cada step genera ~step_duration/avg_solve_time requests
            ramp = sum(RAMP_STEPS) * 3  # ~3 batches por step

        # +1 por warmup
        estimates[mode] = seq + conc + ramp + 1

    return estimates


def _print_summary_table(
    stats: list,
    run_id: str,
) -> None:
    """Imprime tabla resumen en stdout."""
    print(f"\n{'='*80}")
    print(f"  RESUMEN — Run {run_id}")
    print(f"{'='*80}")

    header = f"{'Mode':<15} {'Scenario':<12} {'Nodes':>5} {'Veh':>4} {'C':>3} {'Count':>5} {'p50':>8} {'p95':>8} {'mean':>8} {'Tput':>6} {'OK%':>5}"
    print(header)
    print("-" * len(header))

    for s in stats:
        print(
            f"{s.mode:<15} {s.scenario:<12} {s.num_nodes:>5} "
            f"{s.num_vehicles:>4} {s.concurrency:>3} {s.count:>5} "
            f"{s.p50_ms:>7.0f}ms {s.p95_ms:>7.0f}ms {s.mean_ms:>7.0f}ms "
            f"{s.throughput_rps:>5.1f}/s {s.success_rate*100:>4.0f}%"
        )


def _run_mode(
    mode: str,
    problems_by_key: dict[tuple[int, int], object],
    sizes: list[int],
    vehicles: list[int],
    concurrency: list[int],
    repetitions: int,
    server_url: str | None,
    api_key: str | None,
    skip_ramp: bool,
    concurrency_problem_size: int,
    concurrency_vehicles: int,
    stress_concurrency_levels: list[int] | None = None,
    stress_problem_size: int | None = None,
    stress_vehicles: int | None = None,
) -> list[RequestResult]:
    """Ejecuta todos los escenarios para un modo dado."""
    logger.info("=" * 60)
    logger.info("MODO: %s", mode)
    logger.info("=" * 60)

    runner = BenchmarkRunner(mode=mode, server_url=server_url, api_key=api_key)
    all_results: list[RequestResult] = []

    # Warmup con el problema más pequeño
    smallest_key = (min(sizes), min(vehicles))
    if smallest_key in problems_by_key:
        runner.warmup(problems_by_key[smallest_key])

    # ── Sequential tests ─────────────────────────────────────
    logger.info("--- Sequential tests ---")
    for size in sizes:
        for veh in vehicles:
            key = (size, veh)
            if key not in problems_by_key:
                continue
            problem = problems_by_key[key]
            results = runner.run_sequential([problem], repetitions=repetitions)
            all_results.extend(results)

    # ── Concurrent tests ─────────────────────────────────────
    logger.info("--- Concurrent tests ---")
    for size in sizes:
        for veh in vehicles:
            key = (size, veh)
            if key not in problems_by_key:
                continue
            problem = problems_by_key[key]
            for level in concurrency:
                if level == 1:
                    continue  # Ya cubierto por sequential
                results = runner.run_concurrent(problem, level, repetitions=repetitions)
                all_results.extend(results)

    # ── Ramp-up tests ────────────────────────────────────────
    conc_key = (concurrency_problem_size, concurrency_vehicles)
    if not skip_ramp and mode == "self_hosted" and conc_key in problems_by_key:
        logger.info("--- Ramp-up tests ---")
        ramp_problem = problems_by_key[conc_key]
        results = runner.run_ramp_up(
            ramp_problem,
            steps=RAMP_STEPS,
            step_duration_secs=RAMP_STEP_DURATION_SECS,
        )
        all_results.extend(results)

    # ── High-concurrency stress tests ────────────────────────
    # Solo se activan cuando se pasan stress_concurrency_levels (quick mode).
    # Corren únicamente sobre el problema de stress_problem_size nodos para
    # aislar el efecto de la concurrencia sin mezclar el tamaño del problema.
    if stress_concurrency_levels and stress_problem_size is not None:
        veh = stress_vehicles if stress_vehicles is not None else min(vehicles)
        stress_key = (stress_problem_size, veh)
        if stress_key not in problems_by_key:
            logger.warning(
                "Problema stress (%d nodos, %d vehículos) no encontrado, omitiendo.",
                stress_problem_size,
                veh,
            )
        else:
            stress_problem = problems_by_key[stress_key]
            logger.info(
                "--- High-concurrency stress tests (size=%d, %d nodos) ---",
                stress_problem_size,
                stress_problem_size,
            )
            for level in stress_concurrency_levels:
                results = runner.run_concurrent(
                    stress_problem,
                    level,
                    repetitions=repetitions,
                    bypass_limit=True,
                )
                all_results.extend(results)

    logger.info(
        "Modo %s completado: %d requests totales.",
        mode,
        runner.total_requests,
    )
    return all_results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="cuOpt Performance Benchmarks — SmartWaste MVD",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["api_catalog", "self_hosted", "both"],
        required=True,
        help="Modo de cuOpt a testear",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="URL del servidor cuOpt self-hosted (ej: http://localhost:5000)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key de NVIDIA (default: lee CUOPT_API_KEY del entorno)",
    )
    parser.add_argument(
        "--sizes",
        default=None,
        help="Tamaños de problema separados por coma (default: 10,25,50,100,150,200)",
    )
    parser.add_argument(
        "--concurrency",
        default=None,
        help="Niveles de concurrencia separados por coma (default: 1,2,5,10,20)",
    )
    parser.add_argument(
        "--vehicles",
        default=None,
        help="Cantidades de vehículos separadas por coma (default: 2,4,6)",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=None,
        help=f"Repeticiones por caso de test (default: {DEFAULT_REPETITIONS})",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick mode: subset reducido para pruebas rápidas",
    )
    parser.add_argument(
        "--skip-ramp",
        action="store_true",
        help="Omitir tests de ramp-up",
    )
    parser.add_argument(
        "--containers-csv",
        default=None,
        help="Path al CSV de contenedores para coordenadas reales",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=f"Directorio de salida (default: {RESULTS_DIR})",
    )
    parser.add_argument(
        "--concurrency-problem-size",
        type=int,
        default=100,
        help="Tamaño de problema para tests de concurrencia (default: 100)",
    )
    parser.add_argument(
        "--concurrency-vehicles",
        type=int,
        default=4,
        help="Vehículos para tests de concurrencia (default: 4)",
    )
    args = parser.parse_args()

    # ── Resolver parámetros ──────────────────────────────────
    if args.quick:
        sizes = QUICK_PROBLEM_SIZES
        concurrency = QUICK_CONCURRENCY_LEVELS
        vehicles = QUICK_VEHICLE_COUNTS
        repetitions = QUICK_REPETITIONS
        stress_concurrency_levels: list[int] = QUICK_STRESS_CONCURRENCY_LEVELS
        stress_problem_size: int | None = QUICK_STRESS_PROBLEM_SIZE
        stress_vehicles: int | None = QUICK_STRESS_VEHICLE_COUNT
    else:
        sizes = _parse_int_list(args.sizes) if args.sizes else PROBLEM_SIZES
        concurrency = _parse_int_list(args.concurrency) if args.concurrency else CONCURRENCY_LEVELS
        vehicles = _parse_int_list(args.vehicles) if args.vehicles else VEHICLE_COUNTS
        repetitions = args.repetitions or DEFAULT_REPETITIONS
        stress_concurrency_levels = []
        stress_problem_size = None
        stress_vehicles = None

    modes = ["api_catalog", "self_hosted"] if args.mode == "both" else [args.mode]
    skip_ramp = args.quick or args.skip_ramp
    output_dir = args.output_dir or RESULTS_DIR

    # Validaciones
    if "self_hosted" in modes and not args.server:
        parser.error("--server es requerido para el modo self_hosted")

    # ── Pre-flight: estimar requests ─────────────────────────
    estimates = _estimate_requests(sizes, vehicles, concurrency, repetitions, modes, skip_ramp)
    total = sum(estimates.values())

    print("\n" + "=" * 60)
    print("  cuOpt Performance Benchmark — SmartWaste MVD")
    print("=" * 60)
    print(f"  Modos:         {', '.join(modes)}")
    print(f"  Tamaños:       {sizes}")
    print(f"  Concurrencia:  {concurrency}")
    print(f"  Vehículos:     {vehicles}")
    print(f"  Repeticiones:  {repetitions}")
    print(f"  Skip ramp-up:  {skip_ramp}")
    if stress_concurrency_levels:
        print(f"  Stress test:   C={stress_concurrency_levels} @ {stress_problem_size} nodos (bypass límite API)")
    print()
    for mode, est in estimates.items():
        print(f"  Requests estimados ({mode}): ~{est}")
    print(f"  Total estimado: ~{total}")

    if "api_catalog" in estimates:
        api_est = estimates["api_catalog"]
        if api_est > API_CATALOG_WARN_THRESHOLD:
            print(
                f"\n  ⚠ ADVERTENCIA: {api_est} requests excede el umbral "
                f"de {API_CATALOG_WARN_THRESHOLD}."
            )
            print("  El free tier de NVIDIA permite 5,000 req/mes.")
            response = input("  ¿Continuar? [y/N]: ").strip().lower()
            if response not in ("y", "yes", "s", "si"):
                print("  Abortado.")
                sys.exit(0)

    print()

    # ── Generar problemas ────────────────────────────────────
    csv_path = args.containers_csv
    if not csv_path:
        # Buscar CSV en ubicación por defecto
        default_csv = Path(__file__).resolve().parent.parent.parent / "data" / "processed" / "containers_wgs84.csv"
        if default_csv.exists():
            csv_path = str(default_csv)
            logger.info("Usando CSV de contenedores: %s", csv_path)

    logger.info("Generando problemas VRP...")
    generator = ProblemGenerator(containers_csv=csv_path)

    problems_by_key: dict[tuple[int, int], object] = {}
    for size in sizes:
        for veh in vehicles:
            seed = 42 + size * 100 + veh
            problem = generator.generate(size, veh, seed=seed)
            problems_by_key[(size, veh)] = problem

    # Asegurar que el problema para concurrency existe
    conc_size = args.concurrency_problem_size
    conc_veh = args.concurrency_vehicles
    if (conc_size, conc_veh) not in problems_by_key:
        seed = 42 + conc_size * 100 + conc_veh
        problems_by_key[(conc_size, conc_veh)] = generator.generate(conc_size, conc_veh, seed=seed)

    # Asegurar que el problema de stress existe
    if stress_problem_size is not None and stress_vehicles is not None:
        stress_key = (stress_problem_size, stress_vehicles)
        if stress_key not in problems_by_key:
            seed = 42 + stress_problem_size * 100 + stress_vehicles
            problems_by_key[stress_key] = generator.generate(stress_problem_size, stress_vehicles, seed=seed)

    logger.info("Generados %d problemas.", len(problems_by_key))

    # ── Ejecutar benchmarks ──────────────────────────────────
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    all_results: list[RequestResult] = []

    for mode in modes:
        server = args.server if mode == "self_hosted" else None
        api_key = args.api_key  # CuOptSolver lee del env si es None

        results = _run_mode(
            mode=mode,
            problems_by_key=problems_by_key,
            sizes=sizes,
            vehicles=vehicles,
            concurrency=concurrency,
            repetitions=repetitions,
            server_url=server,
            api_key=api_key,
            skip_ramp=skip_ramp,
            concurrency_problem_size=conc_size,
            concurrency_vehicles=conc_veh,
            stress_concurrency_levels=stress_concurrency_levels,
            stress_problem_size=stress_problem_size,
            stress_vehicles=stress_vehicles,
        )
        all_results.extend(results)

    # ── Resultados ───────────────────────────────────────────
    logger.info("Procesando resultados...")
    collector = ResultCollector(output_dir=output_dir)

    # Guardar resultados crudos
    raw_path = collector.save_raw_results(all_results, run_id)
    logger.info("Resultados crudos: %s", raw_path)

    # Agregar estadísticas
    stats = collector.aggregate(all_results)
    csv_path_out = collector.save_summary_csv(stats, run_id)
    logger.info("Resumen CSV: %s", csv_path_out)

    # Generar gráficas
    logger.info("Generando gráficas...")
    viz = Visualizer(output_dir=output_dir)
    chart_paths = viz.generate_all(stats, all_results, run_id)
    for p in chart_paths:
        logger.info("Gráfica: %s", p)

    # Tabla resumen
    _print_summary_table(stats, run_id)

    print(f"\nResultados guardados en: {output_dir}/")
    print(f"Run ID: {run_id}")


if __name__ == "__main__":
    main()
