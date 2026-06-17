"""
benchmark_runner.py — SmartWaste MVD Performance Tests

Motor de benchmarks para cuOpt. Tres escenarios:
  1. Sequential: requests uno a uno
  2. Concurrent: N requests simultáneos con ThreadPoolExecutor
  3. Ramp-up: incremento gradual de concurrencia (solo self_hosted)
"""

from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Import del solver desde el directorio padre
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vrp_solver import CuOptSolver

from config import (
    API_CATALOG_DELAY_BETWEEN_REQUESTS,
    API_CATALOG_MAX_CONCURRENT,
    API_CATALOG_SAFETY_LIMIT,
    SOLVER_TIME_LIMIT,
)
from problem_generator import VRPProblem
from result_collector import RequestResult

logger = logging.getLogger(__name__)


class BenchmarkRunner:
    """Ejecuta benchmarks contra un solver cuOpt."""

    def __init__(
        self,
        mode: str,
        server_url: str | None = None,
        api_key: str | None = None,
        time_limit: int = SOLVER_TIME_LIMIT,
    ):
        self._mode = mode
        self._time_limit = time_limit
        self._solver = CuOptSolver(mode=mode, server_url=server_url, api_key=api_key)
        self._results: list[RequestResult] = []
        self._total_requests = 0

    @property
    def total_requests(self) -> int:
        return self._total_requests

    def _check_safety_limit(self) -> None:
        """Verifica que no excedamos el límite de seguridad para api_catalog."""
        if (
            self._mode == "api_catalog"
            and self._total_requests >= API_CATALOG_SAFETY_LIMIT
        ):
            raise RuntimeError(
                f"Alcanzado el límite de seguridad de {API_CATALOG_SAFETY_LIMIT} "
                f"requests para api_catalog. Abortando."
            )

    def _solve_single(
        self,
        problem: VRPProblem,
        scenario: str,
        concurrency: int,
        index: int,
    ) -> RequestResult:
        """Ejecuta un solve y mide latencia wall-clock."""
        self._total_requests += 1
        start = time.perf_counter()
        try:
            result = self._solver.solve_vrp(
                cost_matrix=problem.cost_matrix,
                num_vehicles=problem.num_vehicles,
                demands=problem.demands,
                capacities=problem.capacities,
                depot_start_idx=problem.depot_start_idx,
                depot_end_idx=problem.depot_end_idx,
                time_limit=self._time_limit,
            )
            latency_ms = (time.perf_counter() - start) * 1000
            return RequestResult(
                problem_name=problem.name,
                mode=self._mode,
                scenario=scenario,
                concurrency_level=concurrency,
                num_nodes=problem.num_nodes,
                num_vehicles=problem.num_vehicles,
                latency_ms=latency_ms,
                success=result["status"] in ("OPTIMAL", "FEASIBLE"),
                status=result["status"],
                total_cost=result.get("total_cost", 0),
                error_message=None,
                timestamp_iso=datetime.now(timezone.utc).isoformat(),
                request_index=index,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return RequestResult(
                problem_name=problem.name,
                mode=self._mode,
                scenario=scenario,
                concurrency_level=concurrency,
                num_nodes=problem.num_nodes,
                num_vehicles=problem.num_vehicles,
                latency_ms=latency_ms,
                success=False,
                status="ERROR",
                total_cost=0,
                error_message=str(exc)[:300],
                timestamp_iso=datetime.now(timezone.utc).isoformat(),
                request_index=index,
            )

    def warmup(self, problem: VRPProblem) -> None:
        """Envía un request de warmup (no se registra en resultados)."""
        logger.info("Warmup: enviando request de calentamiento...")
        self._solve_single(problem, "warmup", 1, 0)
        self._total_requests -= 1  # No contar el warmup
        logger.info("Warmup completado.")

    def run_sequential(
        self,
        problems: list[VRPProblem],
        repetitions: int = 1,
    ) -> list[RequestResult]:
        """
        Ejecuta problemas uno a uno, midiendo cada request.
        Para api_catalog aplica delay entre requests.
        """
        delay = API_CATALOG_DELAY_BETWEEN_REQUESTS if self._mode == "api_catalog" else 0.0
        results: list[RequestResult] = []

        for rep in range(repetitions):
            for i, problem in enumerate(problems):
                self._check_safety_limit()
                idx = rep * len(problems) + i
                r = self._solve_single(problem, "sequential", 1, idx)
                results.append(r)
                logger.info(
                    "  [seq %d/%d] %s → %s %.0fms",
                    idx + 1,
                    len(problems) * repetitions,
                    problem.name,
                    r.status,
                    r.latency_ms,
                )
                if delay > 0 and idx < len(problems) * repetitions - 1:
                    time.sleep(delay)

        self._results.extend(results)
        return results

    def run_concurrent(
        self,
        problem: VRPProblem,
        concurrency: int,
        repetitions: int = 1,
        bypass_limit: bool = False,
    ) -> list[RequestResult]:
        """
        Envía `concurrency` requests idénticos simultáneamente.
        Para api_catalog, limita a API_CATALOG_MAX_CONCURRENT salvo que
        bypass_limit=True (usado en stress tests de alta concurrencia).
        """
        effective = concurrency
        if self._mode == "api_catalog" and not bypass_limit:
            effective = min(concurrency, API_CATALOG_MAX_CONCURRENT)
            if effective < concurrency:
                logger.warning(
                    "Concurrencia reducida de %d a %d (límite api_catalog)",
                    concurrency,
                    effective,
                )
        elif self._mode == "api_catalog" and bypass_limit:
            logger.warning(
                "Stress test: bypass de límite api_catalog, C=%d (monitorear rate limits)",
                concurrency,
            )

        all_results: list[RequestResult] = []

        for rep in range(repetitions):
            self._check_safety_limit()
            batch_results: list[RequestResult] = []

            with ThreadPoolExecutor(max_workers=effective) as pool:
                futures = {
                    pool.submit(
                        self._solve_single,
                        problem,
                        "concurrent",
                        effective,
                        rep * effective + i,
                    ): i
                    for i in range(effective)
                }
                for future in as_completed(futures):
                    batch_results.append(future.result())

            all_results.extend(batch_results)
            successes = sum(1 for r in batch_results if r.success)
            avg_lat = sum(r.latency_ms for r in batch_results) / len(batch_results)
            logger.info(
                "  [conc C=%d rep=%d] %s → %d/%d ok, avg=%.0fms",
                effective,
                rep + 1,
                problem.name,
                successes,
                effective,
                avg_lat,
            )

            if self._mode == "api_catalog":
                time.sleep(API_CATALOG_DELAY_BETWEEN_REQUESTS)

        self._results.extend(all_results)
        return all_results

    def run_ramp_up(
        self,
        problem: VRPProblem,
        steps: list[int],
        step_duration_secs: float = 30.0,
    ) -> list[RequestResult]:
        """
        Incrementa carga gradualmente. Solo para self_hosted.
        En cada paso, envía requests al nivel de concurrencia dado
        durante step_duration_secs.
        """
        if self._mode == "api_catalog":
            logger.warning("Ramp-up omitido para api_catalog (riesgo de rate limit).")
            return []

        all_results: list[RequestResult] = []

        for level in steps:
            logger.info("  [ramp] Nivel de concurrencia: %d", level)
            step_start = time.monotonic()

            while time.monotonic() - step_start < step_duration_secs:
                batch = []
                with ThreadPoolExecutor(max_workers=level) as pool:
                    futures = {
                        pool.submit(
                            self._solve_single,
                            problem,
                            "ramp_up",
                            level,
                            len(all_results) + i,
                        ): i
                        for i in range(level)
                    }
                    for future in as_completed(futures):
                        batch.append(future.result())

                all_results.extend(batch)

        self._results.extend(all_results)
        return all_results

    def get_all_results(self) -> list[RequestResult]:
        """Retorna todos los resultados acumulados."""
        return list(self._results)
