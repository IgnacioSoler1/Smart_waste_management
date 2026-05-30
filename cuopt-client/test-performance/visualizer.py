"""
visualizer.py — SmartWaste MVD Performance Tests

Genera gráficas de performance a partir de resultados de benchmarks.
5 tipos de gráfica:
  1. Latency vs Problem Size
  2. Latency vs Concurrency
  3. Throughput vs Concurrency
  4. Latency Distribution (box plot)
  5. Success Rate vs Load
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from config import RESULTS_DIR
from result_collector import AggregatedStats, RequestResult


# Colores por modo
COLORS = {
    "api_catalog": "#76B900",  # NVIDIA green
    "self_hosted": "#1E88E5",  # Blue
}

MODE_LABELS = {
    "api_catalog": "API Catalog (Cloud)",
    "self_hosted": "Self-Hosted (EC2 GPU)",
}


class Visualizer:
    """Genera gráficas PNG de resultados de performance."""

    def __init__(self, output_dir: str | None = None):
        self._output_dir = Path(output_dir or RESULTS_DIR)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        plt.style.use("seaborn-v0_8-whitegrid")

    def plot_latency_vs_problem_size(
        self,
        stats: list[AggregatedStats],
        vehicles: int = 4,
        run_id: str = "",
    ) -> Path:
        """Latencia (p50) vs tamaño del problema. Una línea por modo."""
        fig, ax = plt.subplots(figsize=(10, 6))

        for mode in ("api_catalog", "self_hosted"):
            filtered = sorted(
                [
                    s
                    for s in stats
                    if s.mode == mode
                    and s.scenario == "sequential"
                    and s.num_vehicles == vehicles
                    and s.concurrency == 1
                ],
                key=lambda s: s.num_nodes,
            )
            if not filtered:
                continue

            x = [s.num_nodes for s in filtered]
            y_p50 = [s.p50_ms for s in filtered]
            y_p95 = [s.p95_ms for s in filtered]

            color = COLORS.get(mode, "#666666")
            label = MODE_LABELS.get(mode, mode)
            ax.plot(x, y_p50, marker="o", color=color, label=f"{label} (p50)", linewidth=2)
            ax.plot(
                x, y_p95, marker="s", color=color, label=f"{label} (p95)",
                linewidth=1, linestyle="--", alpha=0.7,
            )

        ax.set_xlabel("Problem Size (nodes)", fontsize=12)
        ax.set_ylabel("Latency (ms)", fontsize=12)
        ax.set_title(f"Latency vs Problem Size ({vehicles} vehicles)", fontsize=14)
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)

        path = self._output_dir / f"latency_vs_size_{run_id}.png"
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    def plot_latency_vs_concurrency(
        self,
        stats: list[AggregatedStats],
        num_nodes: int = 100,
        vehicles: int = 4,
        run_id: str = "",
    ) -> Path:
        """Latencia (p50) vs nivel de concurrencia. Una línea por modo."""
        fig, ax = plt.subplots(figsize=(10, 6))

        for mode in ("api_catalog", "self_hosted"):
            filtered = sorted(
                [
                    s
                    for s in stats
                    if s.mode == mode
                    and s.scenario == "concurrent"
                    and s.num_nodes == num_nodes
                    and s.num_vehicles == vehicles
                ],
                key=lambda s: s.concurrency,
            )
            if not filtered:
                continue

            x = [s.concurrency for s in filtered]
            y = [s.p50_ms for s in filtered]
            color = COLORS.get(mode, "#666666")
            label = MODE_LABELS.get(mode, mode)
            ax.plot(x, y, marker="o", color=color, label=label, linewidth=2)

        ax.set_xlabel("Concurrent Requests", fontsize=12)
        ax.set_ylabel("Latency p50 (ms)", fontsize=12)
        ax.set_title(
            f"Latency vs Concurrency ({num_nodes} nodes, {vehicles} vehicles)",
            fontsize=14,
        )
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)

        path = self._output_dir / f"latency_vs_concurrency_{run_id}.png"
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    def plot_throughput_vs_concurrency(
        self,
        stats: list[AggregatedStats],
        num_nodes: int = 100,
        vehicles: int = 4,
        run_id: str = "",
    ) -> Path:
        """Throughput (req/s) vs concurrencia. Barras agrupadas por modo."""
        fig, ax = plt.subplots(figsize=(10, 6))

        modes_data: dict[str, tuple[list[int], list[float]]] = {}
        for mode in ("api_catalog", "self_hosted"):
            filtered = sorted(
                [
                    s
                    for s in stats
                    if s.mode == mode
                    and s.scenario == "concurrent"
                    and s.num_nodes == num_nodes
                    and s.num_vehicles == vehicles
                ],
                key=lambda s: s.concurrency,
            )
            if filtered:
                modes_data[mode] = (
                    [s.concurrency for s in filtered],
                    [s.throughput_rps for s in filtered],
                )

        if not modes_data:
            plt.close(fig)
            return self._output_dir / f"throughput_vs_concurrency_{run_id}.png"

        # Unificar los niveles de concurrencia
        all_levels = sorted(set(c for xs, _ in modes_data.values() for c in xs))
        x_pos = np.arange(len(all_levels))
        bar_width = 0.35

        for i, mode in enumerate(("api_catalog", "self_hosted")):
            if mode not in modes_data:
                continue
            levels, throughputs = modes_data[mode]
            # Mapear a posiciones unificadas
            y = [throughputs[levels.index(l)] if l in levels else 0 for l in all_levels]
            offset = (i - 0.5) * bar_width
            color = COLORS.get(mode, "#666666")
            label = MODE_LABELS.get(mode, mode)
            ax.bar(x_pos + offset, y, bar_width, label=label, color=color, alpha=0.85)

        ax.set_xlabel("Concurrent Requests", fontsize=12)
        ax.set_ylabel("Throughput (req/s)", fontsize=12)
        ax.set_title(
            f"Throughput vs Concurrency ({num_nodes} nodes, {vehicles} vehicles)",
            fontsize=14,
        )
        ax.set_xticks(x_pos)
        ax.set_xticklabels([str(l) for l in all_levels])
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3, axis="y")

        path = self._output_dir / f"throughput_vs_concurrency_{run_id}.png"
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    def plot_latency_distribution(
        self,
        raw_results: list[RequestResult],
        scenario: str = "sequential",
        vehicles: int = 4,
        run_id: str = "",
    ) -> Path:
        """Box plot de distribución de latencias por (modo, tamaño)."""
        fig, ax = plt.subplots(figsize=(12, 6))

        # Agrupar por (mode, num_nodes)
        groups: dict[tuple[str, int], list[float]] = {}
        for r in raw_results:
            if r.scenario == scenario and r.num_vehicles == vehicles:
                key = (r.mode, r.num_nodes)
                groups.setdefault(key, []).append(r.latency_ms)

        if not groups:
            plt.close(fig)
            return self._output_dir / f"latency_distribution_{run_id}.png"

        # Ordenar por num_nodes, luego por modo
        sorted_keys = sorted(groups.keys(), key=lambda k: (k[1], k[0]))
        data = [groups[k] for k in sorted_keys]
        labels = [f"{MODE_LABELS.get(k[0], k[0])[:10]}\n{k[1]}n" for k in sorted_keys]
        colors = [COLORS.get(k[0], "#666666") for k in sorted_keys]

        bp = ax.boxplot(data, labels=labels, patch_artist=True)
        for patch, color in zip(bp["boxes"], colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.6)

        ax.set_xlabel("Mode / Problem Size", fontsize=12)
        ax.set_ylabel("Latency (ms)", fontsize=12)
        ax.set_title(f"Latency Distribution ({scenario}, {vehicles} vehicles)", fontsize=14)
        ax.grid(True, alpha=0.3, axis="y")
        plt.xticks(rotation=45, ha="right", fontsize=9)

        path = self._output_dir / f"latency_distribution_{run_id}.png"
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    def plot_success_rate(
        self,
        stats: list[AggregatedStats],
        num_nodes: int = 100,
        vehicles: int = 4,
        run_id: str = "",
    ) -> Path:
        """Tasa de éxito vs concurrencia. Una línea por modo."""
        fig, ax = plt.subplots(figsize=(10, 6))

        for mode in ("api_catalog", "self_hosted"):
            filtered = sorted(
                [
                    s
                    for s in stats
                    if s.mode == mode
                    and s.scenario == "concurrent"
                    and s.num_nodes == num_nodes
                    and s.num_vehicles == vehicles
                ],
                key=lambda s: s.concurrency,
            )
            if not filtered:
                continue

            x = [s.concurrency for s in filtered]
            y = [s.success_rate * 100 for s in filtered]
            color = COLORS.get(mode, "#666666")
            label = MODE_LABELS.get(mode, mode)
            ax.plot(x, y, marker="o", color=color, label=label, linewidth=2)

        ax.set_xlabel("Concurrent Requests", fontsize=12)
        ax.set_ylabel("Success Rate (%)", fontsize=12)
        ax.set_title(
            f"Success Rate vs Concurrency ({num_nodes} nodes, {vehicles} vehicles)",
            fontsize=14,
        )
        ax.set_ylim(-5, 105)
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)

        path = self._output_dir / f"success_rate_{run_id}.png"
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    def generate_all(
        self,
        stats: list[AggregatedStats],
        raw_results: list[RequestResult],
        run_id: str,
    ) -> list[Path]:
        """Genera las 5 gráficas y retorna sus paths."""
        paths = []

        # Determinar vehículos más comunes en los resultados
        vehicle_counts = {}
        for s in stats:
            vehicle_counts[s.num_vehicles] = vehicle_counts.get(s.num_vehicles, 0) + 1
        vehicles = max(vehicle_counts, key=vehicle_counts.get) if vehicle_counts else 4

        # Determinar tamaño de problema más común para tests de concurrencia
        node_counts = {}
        for s in stats:
            if s.scenario == "concurrent":
                node_counts[s.num_nodes] = node_counts.get(s.num_nodes, 0) + 1
        num_nodes = max(node_counts, key=node_counts.get) if node_counts else 100

        paths.append(self.plot_latency_vs_problem_size(stats, vehicles=vehicles, run_id=run_id))
        paths.append(self.plot_latency_vs_concurrency(stats, num_nodes=num_nodes, vehicles=vehicles, run_id=run_id))
        paths.append(self.plot_throughput_vs_concurrency(stats, num_nodes=num_nodes, vehicles=vehicles, run_id=run_id))
        paths.append(self.plot_latency_distribution(raw_results, vehicles=vehicles, run_id=run_id))
        paths.append(self.plot_success_rate(stats, num_nodes=num_nodes, vehicles=vehicles, run_id=run_id))

        return paths
