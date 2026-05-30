"""
result_collector.py — SmartWaste MVD Performance Tests

Agrega resultados de benchmarks, calcula estadísticas y persiste a disco.
"""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path

from config import RESULTS_DIR


@dataclass
class RequestResult:
    """Resultado de una solicitud individual al solver."""

    problem_name: str
    mode: str  # "api_catalog" | "self_hosted"
    scenario: str  # "sequential" | "concurrent" | "ramp_up"
    concurrency_level: int
    num_nodes: int
    num_vehicles: int
    latency_ms: float
    success: bool
    status: str  # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "ERROR" | "TIMEOUT"
    total_cost: float
    error_message: str | None
    timestamp_iso: str
    request_index: int


@dataclass
class AggregatedStats:
    """Estadísticas agregadas para un grupo de resultados."""

    mode: str
    scenario: str
    num_nodes: int
    num_vehicles: int
    concurrency: int
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    min_ms: float
    max_ms: float
    std_ms: float
    throughput_rps: float
    success_rate: float


class ResultCollector:
    """Recopila, agrega y persiste resultados de benchmarks."""

    def __init__(self, output_dir: str | None = None):
        self._output_dir = Path(output_dir or RESULTS_DIR)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def save_raw_results(
        self, results: list[RequestResult], run_id: str
    ) -> Path:
        """Guarda resultados crudos como JSON."""
        path = self._output_dir / f"raw_{run_id}.json"
        data = [asdict(r) for r in results]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return path

    def aggregate(self, results: list[RequestResult]) -> list[AggregatedStats]:
        """
        Agrupa por (mode, scenario, num_nodes, num_vehicles, concurrency)
        y calcula percentiles.
        """
        groups: dict[tuple, list[RequestResult]] = {}
        for r in results:
            key = (r.mode, r.scenario, r.num_nodes, r.num_vehicles, r.concurrency_level)
            groups.setdefault(key, []).append(r)

        stats_list: list[AggregatedStats] = []
        for key, group in sorted(groups.items()):
            latencies = [r.latency_ms for r in group]
            successes = [r for r in group if r.success]
            sorted_lat = sorted(latencies)

            # Throughput: requests exitosos / tiempo total del batch
            total_time_s = sum(latencies) / 1000.0
            concurrency = key[4]
            if total_time_s > 0 and concurrency > 0:
                # Con concurrencia, el wall time es ~total_time / concurrency
                wall_time_s = total_time_s / concurrency
                throughput = len(successes) / wall_time_s if wall_time_s > 0 else 0.0
            else:
                throughput = 0.0

            std = statistics.stdev(latencies) if len(latencies) > 1 else 0.0

            stats_list.append(
                AggregatedStats(
                    mode=key[0],
                    scenario=key[1],
                    num_nodes=key[2],
                    num_vehicles=key[3],
                    concurrency=key[4],
                    count=len(group),
                    p50_ms=_percentile(sorted_lat, 50),
                    p95_ms=_percentile(sorted_lat, 95),
                    p99_ms=_percentile(sorted_lat, 99),
                    mean_ms=statistics.mean(latencies),
                    min_ms=min(latencies),
                    max_ms=max(latencies),
                    std_ms=std,
                    throughput_rps=throughput,
                    success_rate=len(successes) / len(group) if group else 0.0,
                )
            )

        return stats_list

    def save_summary_csv(
        self, stats: list[AggregatedStats], run_id: str
    ) -> Path:
        """Guarda estadísticas agregadas como CSV."""
        path = self._output_dir / f"summary_{run_id}.csv"
        if not stats:
            return path

        fieldnames = [
            "mode", "scenario", "num_nodes", "num_vehicles", "concurrency",
            "count", "p50_ms", "p95_ms", "p99_ms", "mean_ms",
            "min_ms", "max_ms", "std_ms", "throughput_rps", "success_rate",
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for s in stats:
                row = asdict(s)
                # Redondear floats a 2 decimales
                for k in row:
                    if isinstance(row[k], float):
                        row[k] = round(row[k], 2)
                writer.writerow(row)

        return path


def _percentile(sorted_data: list[float], pct: int) -> float:
    """Calcula percentil de datos ya ordenados."""
    if not sorted_data:
        return 0.0
    if len(sorted_data) == 1:
        return sorted_data[0]
    k = (len(sorted_data) - 1) * pct / 100
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])
