"""
test_depot_and_baseline.py — Tests del mapeo de depósitos y del baseline estático.

Cubre:
  - _resolve_depot: ids reales de data/processed, alias legacy y fallback por coordenadas
  - _candidate_nodes / _submatrix: el solver recibe solo candidatos (fill > 20%)
  - _optimize_circuit: el baseline recorre TODOS los activos y el depósito
    corresponde a la zona del circuito
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# conftest.py prepara env vars + mocks de boto3 antes del import
import handler  # noqa: E402
from handler import (  # noqa: E402
    _DEPOTS,
    _candidate_nodes,
    _resolve_depot,
    _submatrix,
)


def _container(seq: int, fill: float, depot_id: str = "depot_estacion_transferencia") -> dict:
    return {
        "container_id": f"W-c{seq}",
        "circuit_id":   "W",
        "latitude":     -34.85 + seq * 0.001,
        "longitude":    -56.25 + seq * 0.001,
        "fill_level":   fill,
        "depot_id":     depot_id,
        "csv_sequence": seq,
        "status":       "active",
    }


class TestResolveDepot:

    def test_real_data_ids(self):
        assert _resolve_depot({"depot_id": "depot_felipe_cardoso"}) == "felipe_cardoso"
        assert _resolve_depot({"depot_id": "depot_estacion_transferencia"}) == "ruta_102"

    def test_legacy_keys(self):
        assert _resolve_depot({"depot_id": "felipe_cardoso"}) == "felipe_cardoso"
        assert _resolve_depot({"depot_id": "ruta_102"}) == "ruta_102"

    def test_coordinates_fallback(self):
        # Ítems sembrados antes del fix: sin depot_id, solo coordenadas del depósito
        lat, lon = _DEPOTS["ruta_102"]
        assert _resolve_depot({"depot_lat": lat, "depot_lon": lon}) == "ruta_102"
        lat, lon = _DEPOTS["felipe_cardoso"]
        assert _resolve_depot({"depot_lat": lat, "depot_lon": lon}) == "felipe_cardoso"

    def test_unknown_falls_back_to_default(self):
        assert _resolve_depot({}) == "felipe_cardoso"
        assert _resolve_depot({"depot_lat": 0, "depot_lon": 0}) == "felipe_cardoso"


class TestCandidateSubproblem:

    def test_candidate_nodes_skip_low_fill(self):
        containers = [_container(0, 80), _container(1, 10), _container(2, 50), _container(3, 20)]
        # matriz completa: 0=depot, 1..4=containers, 5=depot_end
        assert _candidate_nodes(containers) == [0, 1, 3, 5]

    def test_submatrix(self):
        m = [[10 * i + j for j in range(4)] for i in range(4)]
        assert _submatrix(m, [0, 2, 3]) == [[0, 2, 3], [20, 22, 23], [30, 32, 33]]


class TestOptimizeCircuitBaseline:

    def test_baseline_covers_all_active_and_solver_gets_candidates(self):
        # 6 candidatos (>60%) + 3 de baja carga (≤20%)
        containers = [_container(i, 85.0) for i in range(6)] + [
            _container(6 + i, 5.0) for i in range(3)
        ]
        n_full = len(containers) + 2
        durations = [[abs(i - j) * 60 for j in range(n_full)] for i in range(n_full)]
        distances = [[abs(i - j) * 500.0 for j in range(n_full)] for i in range(n_full)]

        fake_osrm = MagicMock()
        fake_osrm.get_road_bearings.return_value = None
        fake_osrm.get_distance_matrix.return_value = {
            "durations": durations, "distances": distances, "n": n_full,
        }

        captured: dict = {}

        class _FakeSolver:
            def solve_vrp(self, **kw):
                captured["solver_kwargs"] = kw
                n = len(kw["cost_matrix"])
                return {"routes": {0: list(range(n))}, "total_cost": 1.0,
                        "status": "FEASIBLE", "solver": "fake"}

        def _fake_save_route(**kw):
            captured["save_kwargs"] = kw
            return "route-1", 0.0, 0.0

        with patch.object(handler, "_get_containers", return_value=containers), \
             patch.object(handler, "_get_idle_trucks", return_value=[]), \
             patch.object(handler, "_osrm", fake_osrm), \
             patch.object(handler, "_make_solver", return_value=_FakeSolver()), \
             patch.object(handler, "_supersede_routes"), \
             patch.object(handler, "_save_route", side_effect=_fake_save_route):
            result = handler._optimize_circuit("W")

        assert result["status"] == "optimized"

        # OSRM se pidió sobre todos los activos (9 + 2 depósitos)
        locations = fake_osrm.get_distance_matrix.call_args[0][0]
        assert len(locations) == n_full
        # Depósito de la zona oeste
        assert locations[0] == _DEPOTS["ruta_102"]

        # El solver recibe solo los 6 candidatos + 2 depósitos
        assert len(captured["solver_kwargs"]["cost_matrix"]) == 8
        assert len(captured["save_kwargs"]["containers"]) == 6

        # Baseline: depot → 9 contenedores en orden CSV → depot_end
        baseline = captured["save_kwargs"]["baseline"]
        assert baseline["baseline_stops"] == 9
        # nodos 0,1,...,9,10 consecutivos → 10 arcos de 60 s
        assert baseline["baseline_duration_s"] == 10 * 60
