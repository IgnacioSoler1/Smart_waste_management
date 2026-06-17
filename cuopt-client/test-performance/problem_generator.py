"""
problem_generator.py — SmartWaste MVD Performance Tests

Genera problemas VRP realistas a distintas escalas usando coordenadas
reales de contenedores de Montevideo.
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from constraints import estimate_demand_kg

from config import (
    CONTAINER_CAPACITY_LITERS,
    DEPOT_LAT,
    DEPOT_LON,
    URBAN_SPEED_MS,
    VEHICLE_CAPACITY_KG,
)


@dataclass
class VRPProblem:
    """Instancia completa de un problema VRP lista para el solver."""

    name: str
    num_nodes: int  # incluye depot
    num_vehicles: int
    cost_matrix: list[list[int]]
    demands: list[float]
    capacities: list[float]
    depot_start_idx: int = 0
    depot_end_idx: int = 0
    coordinates: list[tuple[float, float]] = field(default_factory=list)


class ProblemGenerator:
    """Genera problemas VRP con coordenadas reales de Montevideo."""

    def __init__(self, containers_csv: Optional[str] = None):
        self._containers: list[tuple[float, float]] = []
        if containers_csv:
            self._load_containers(containers_csv)

    def _load_containers(self, csv_path: str) -> None:
        """Carga coordenadas (lat, lon) del CSV de contenedores."""
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"CSV no encontrado: {csv_path}")

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    lat = float(row["latitude"])
                    lon = float(row["longitude"])
                    if row.get("coord_valid", "True").strip() == "True":
                        self._containers.append((lat, lon))
                except (ValueError, KeyError):
                    continue

    def generate(
        self,
        num_nodes: int,
        num_vehicles: int,
        seed: int = 42,
    ) -> VRPProblem:
        """
        Genera un problema VRP con num_nodes ubicaciones.

        - Nodo 0 = depot (Felipe Cardoso)
        - Nodos 1..N-1 = contenedores (sampled de coordenadas reales o generados)
        - Cost matrix = haversine en metros
        - Demands basados en fill levels aleatorios [30%, 100%]
        """
        rng = random.Random(seed)
        num_containers = num_nodes - 1

        # Coordenadas: depot + contenedores
        coords: list[tuple[float, float]] = [(DEPOT_LAT, DEPOT_LON)]

        if self._containers and len(self._containers) >= num_containers:
            # Samplear contenedores reales sin repetición
            sampled = rng.sample(self._containers, num_containers)
            coords.extend(sampled)
        else:
            # Generar coordenadas sintéticas alrededor de Montevideo
            coords.extend(self._generate_synthetic_coords(num_containers, rng))

        cost_matrix = self._build_cost_matrix(coords)
        demands = self._generate_demands(num_nodes, rng)
        capacities = self._adjust_capacities(demands, num_vehicles)

        return VRPProblem(
            name=f"mvd_{num_nodes}n_{num_vehicles}v",
            num_nodes=num_nodes,
            num_vehicles=num_vehicles,
            cost_matrix=cost_matrix,
            demands=demands,
            capacities=capacities,
            coordinates=coords,
        )

    def generate_batch(
        self,
        sizes: list[int],
        vehicle_counts: list[int],
        seed: int = 42,
    ) -> list[VRPProblem]:
        """Genera todas las combinaciones de (tamaño, vehículos)."""
        problems = []
        for size in sizes:
            for vehicles in vehicle_counts:
                # Seed único por combinación para reproducibilidad
                combo_seed = seed + size * 100 + vehicles
                problems.append(self.generate(size, vehicles, combo_seed))
        return problems

    def _generate_synthetic_coords(
        self, n: int, rng: random.Random
    ) -> list[tuple[float, float]]:
        """Genera coordenadas en la zona urbana de Montevideo."""
        # Bounding box aproximado de Montevideo urbano
        lat_min, lat_max = -34.93, -34.83
        lon_min, lon_max = -56.28, -56.05
        return [
            (rng.uniform(lat_min, lat_max), rng.uniform(lon_min, lon_max))
            for _ in range(n)
        ]

    @staticmethod
    def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
        """Distancia haversine en metros (entero)."""
        R = 6_371_000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = (
            math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        )
        return int(2 * R * math.asin(math.sqrt(a)))

    def _build_cost_matrix(
        self, coords: list[tuple[float, float]]
    ) -> list[list[int]]:
        """
        Construye matriz N x N de tiempos de viaje en segundos.

        Usa haversine para distancia en metros, luego convierte a segundos
        con velocidad urbana promedio (30 km/h). Esto hace que la cost_matrix
        sea coherente con la ventana de tiempo de vrp_solver._build_payload()
        (time_limit * 10_000 segundos).
        """
        n = len(coords)
        return [
            [
                int(self._haversine_m(coords[i][0], coords[i][1], coords[j][0], coords[j][1]) / URBAN_SPEED_MS)
                for j in range(n)
            ]
            for i in range(n)
        ]

    @staticmethod
    def _generate_demands(n: int, rng: random.Random) -> list[float]:
        """
        Genera demands realistas.
        Nodo 0 (depot) = 0. Otros: fill_level aleatorio [30, 100] -> kg.
        """
        demands = [0.0]  # depot
        for _ in range(n - 1):
            fill_level = rng.uniform(30.0, 100.0)
            demands.append(estimate_demand_kg(fill_level, CONTAINER_CAPACITY_LITERS))
        return demands

    @staticmethod
    def _adjust_capacities(
        demands: list[float], num_vehicles: int
    ) -> list[float]:
        """Ajusta capacidades para garantizar feasibility (margen 50%)."""
        total_demand = sum(demands)
        # Margen generoso (50%) para que cuOpt siempre encuentre solución
        min_capacity_per_vehicle = (total_demand * 1.5) / num_vehicles
        capacity = max(VEHICLE_CAPACITY_KG, min_capacity_per_vehicle)
        return [capacity] * num_vehicles
