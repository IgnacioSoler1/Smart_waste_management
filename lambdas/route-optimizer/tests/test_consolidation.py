"""
test_consolidation.py — Tests para el flujo multi-circuito consolidado.

Cubre las funciones puras del handler:
  - _group_circuits_by_depot_shift: bin-packing por depot + OSRM size cap
  - _baseline_for_origin_circuit: baseline por origin usando matriz consolidada
  - integración: dominant_circuit_id resolution + lambda_handler dispatching
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# conftest.py prepara env vars + mocks de boto3 antes del import
from handler import (  # noqa: E402
    CircuitGroup,
    _baseline_for_origin_circuit,
    _group_circuits_by_depot_shift,
    lambda_handler,
)


# ─────────────────────────────────────────────────────────
# Helpers para fabricar circuit_data fixture
# ─────────────────────────────────────────────────────────

def _make_container(circuit_id: str, csv_seq: int, fill: float = 75.0) -> dict:
    """Genera un container minimal para tests; coords sintéticas en MVD."""
    return {
        "container_id":     f"{circuit_id}-c{csv_seq}",
        "circuit_id":       circuit_id,
        "latitude":         -34.85 + csv_seq * 0.0005,
        "longitude":        -56.15 + csv_seq * 0.0005,
        "fill_level":       fill,
        "depot_id":         "felipe_cardoso",
        "shift":            "morning",
        "csv_sequence":     csv_seq,
        "capacity_liters":  2400,
    }


def _make_circuit_data(
    circuit_id: str,
    n: int,
    depot_id: str = "felipe_cardoso",
    fill: float = 75.0,
) -> dict:
    """Genera el dict que produce _prefetch_circuit_data para un circuit."""
    containers = [_make_container(circuit_id, i, fill) for i in range(n)]
    for c in containers:
        c["depot_id"] = depot_id
    return {
        "containers":  containers,
        "trucks":      [],
        "depot_id":    depot_id,
        "n_total":     n,
        "n_mandatory": sum(1 for c in containers if c["fill_level"] > 60),
    }


# ─────────────────────────────────────────────────────────
# Tests: _group_circuits_by_depot_shift
# ─────────────────────────────────────────────────────────

class TestGrouping:

    def test_single_depot_three_circuits_one_group(self):
        """3 circuitos del mismo depot que entran dentro del cap → 1 grupo."""
        data = {
            "A": _make_circuit_data("A", 80),
            "B": _make_circuit_data("B", 90),
            "C": _make_circuit_data("C", 70),
        }
        groups = _group_circuits_by_depot_shift(data, "morning")
        assert len(groups) == 1
        assert set(groups[0].circuit_ids) == {"A", "B", "C"}
        assert groups[0].depot_id == "felipe_cardoso"
        assert groups[0].total_containers == 240

    def test_two_depots_two_groups(self):
        """Circuitos de depots diferentes → grupos separados (uno por depot)."""
        data = {
            "A": _make_circuit_data("A", 80, depot_id="felipe_cardoso"),
            "B": _make_circuit_data("B", 80, depot_id="ruta_102"),
            "C": _make_circuit_data("C", 80, depot_id="felipe_cardoso"),
        }
        groups = _group_circuits_by_depot_shift(data, "morning")
        assert len(groups) == 2
        depot_to_circuits = {g.depot_id: set(g.circuit_ids) for g in groups}
        assert depot_to_circuits["felipe_cardoso"] == {"A", "C"}
        assert depot_to_circuits["ruta_102"] == {"B"}

    def test_size_cap_splits_into_subgroups(self):
        """Grupo total > cap → bin-packing en sub-grupos."""
        # 5 circuitos de 150 containers cada uno = 750 total > 498 (500-2)
        data = {chr(ord("A") + i): _make_circuit_data(chr(ord("A") + i), 150)
                for i in range(5)}
        groups = _group_circuits_by_depot_shift(data, "morning", max_locations_per_group=500)
        # Cap efectivo de containers = 498. 150 cada uno → 3 caben por grupo (450)
        # Esperamos 2 grupos: [3 circuitos × 150] + [2 circuitos × 150]
        assert len(groups) == 2
        sizes = sorted([g.total_containers for g in groups])
        assert sizes == [300, 450]
        for g in groups:
            assert g.total_containers <= 498

    def test_oversized_single_circuit_gets_own_group(self):
        """Un único circuito > cap → grupo solo (caerá a single-circuit fallback)."""
        data = {
            "BIG": _make_circuit_data("BIG", 600),     # > 498
            "SMALL": _make_circuit_data("SMALL", 50),  # cabe solo
        }
        groups = _group_circuits_by_depot_shift(data, "morning", max_locations_per_group=500)
        # BIG en su propio grupo, SMALL en otro
        assert len(groups) == 2
        sizes = sorted([g.total_containers for g in groups])
        assert sizes == [50, 600]
        big_group = next(g for g in groups if g.total_containers == 600)
        assert big_group.circuit_ids == ["BIG"]  # solo en su grupo

    def test_empty_input(self):
        groups = _group_circuits_by_depot_shift({}, "morning")
        assert groups == []

    def test_group_id_format(self):
        """group_id incluye shift + depot + index para trazabilidad."""
        data = {
            "A": _make_circuit_data("A", 80),
            "B": _make_circuit_data("B", 80),
        }
        groups = _group_circuits_by_depot_shift(data, "morning")
        assert all(g.group_id.startswith("morning-felipe_cardoso-") for g in groups)


# ─────────────────────────────────────────────────────────
# Tests: _baseline_for_origin_circuit
# ─────────────────────────────────────────────────────────

class TestBaselineByOrigin:

    def test_filters_by_circuit_id(self):
        """Solo cuenta containers de origen X, ignora resto del grupo."""
        containers = [
            _make_container("A", 0),
            _make_container("B", 0),
            _make_container("A", 1),
            _make_container("B", 1),
        ]
        # Matriz dummy uniforme: 100s entre cualquier par, 0 en diagonal y depots
        n = len(containers) + 2  # depot_start + 4 containers + depot_end
        matrix_s = [[100 if i != j else 0 for j in range(n)] for i in range(n)]
        matrix_m = [[100.0 if i != j else 0.0 for j in range(n)] for i in range(n)]

        bl = _baseline_for_origin_circuit(
            containers_global=containers,
            origin_circuit_id="A",
            durations_matrix=matrix_s,
            distances_matrix=matrix_m,
            depot_start_idx=0,
            depot_end_idx=n - 1,
        )
        # A aporta 2 containers → tour = depot_start → c0_A → c2_A → depot_end (3 arcos)
        assert bl["baseline_stops"] == 2
        assert bl["baseline_duration_s"] == 300  # 3 arcos * 100s

    def test_empty_when_circuit_absent(self):
        """Circuit no presente → returns zeroes."""
        containers = [_make_container("A", 0), _make_container("B", 0)]
        n = len(containers) + 2
        m = [[0] * n for _ in range(n)]
        bl = _baseline_for_origin_circuit(
            containers_global=containers,
            origin_circuit_id="NOT_THERE",
            durations_matrix=m,
            distances_matrix=[[0.0] * n for _ in range(n)],
            depot_start_idx=0,
            depot_end_idx=n - 1,
        )
        assert bl["baseline_stops"] == 0
        assert bl["baseline_duration_s"] == 0.0

    def test_respects_csv_sequence_order(self):
        """Tour debe seguir el orden de csv_sequence asignado por la Intendencia."""
        # Containers de A con csv_sequence desordenado en el array global
        containers = [
            _make_container("A", 2),  # global idx 0 → matrix idx 1
            _make_container("A", 0),  # global idx 1 → matrix idx 2
            _make_container("A", 1),  # global idx 2 → matrix idx 3
        ]
        n = len(containers) + 2
        # Matriz triangular asimétrica para detectar el orden
        matrix_s = [[abs(i - j) * 10 for j in range(n)] for i in range(n)]
        bl = _baseline_for_origin_circuit(
            containers_global=containers,
            origin_circuit_id="A",
            durations_matrix=matrix_s,
            distances_matrix=[[float(v) for v in row] for row in matrix_s],
            depot_start_idx=0,
            depot_end_idx=n - 1,
        )
        # Orden CSV: csv_seq=0,1,2 → matrix indices 2, 3, 1
        # Tour: 0 (depot_start) → 2 → 3 → 1 → 4 (depot_end)
        # Costos: |0-2|+|2-3|+|3-1|+|1-4| = 20+10+20+30 = 80
        assert bl["baseline_duration_s"] == 80
        assert bl["baseline_stops"] == 3


# ─────────────────────────────────────────────────────────
# Tests: lambda_handler dispatching
# ─────────────────────────────────────────────────────────

class TestLambdaDispatching:

    def test_explicit_circuit_id_uses_single_path(self):
        """Event con circuit_id → llama _optimize_circuit directamente."""
        with patch("handler._optimize_circuit") as mock_single, \
             patch("handler._optimize_circuit_group") as mock_group:
            mock_single.return_value = {"circuit_id": "X", "status": "optimized"}
            response = lambda_handler({"circuit_id": "X"}, MagicMock(aws_request_id="t"))

        mock_single.assert_called_once_with("X")
        mock_group.assert_not_called()
        assert response["statusCode"] == 200
        assert response["circuits"] == 1

    def test_shift_event_groups_and_dispatches(self):
        """Event con shift → prefetch + group; grupo de 1 cae a single, grupos de ≥2 consolidan."""
        with patch("handler._get_circuits_for_shift", return_value=["A", "B", "C"]), \
             patch("handler._prefetch_circuit_data") as mock_prefetch, \
             patch("handler._optimize_circuit") as mock_single, \
             patch("handler._optimize_circuit_group") as mock_group:

            # A y B comparten felipe_cardoso, C va a ruta_102 (queda solo)
            mock_prefetch.return_value = {
                "A": _make_circuit_data("A", 50, "felipe_cardoso"),
                "B": _make_circuit_data("B", 50, "felipe_cardoso"),
                "C": _make_circuit_data("C", 50, "ruta_102"),
            }
            mock_single.return_value = {"circuit_id": "C", "status": "optimized"}
            mock_group.return_value = [
                {"circuit_id": "A", "status": "optimized", "consolidated": True},
                {"circuit_id": "B", "status": "optimized", "consolidated": True},
            ]

            response = lambda_handler({"shift": "morning"}, MagicMock(aws_request_id="t"))

        # 1 grupo de 2 (consolidado) + 1 grupo de 1 (single-circuit fallback)
        assert mock_group.call_count == 1
        assert mock_single.call_count == 1
        assert mock_single.call_args[0][0] == "C"
        assert response["consolidated"] == 2  # A y B
        assert response["circuits"] == 3      # A, B, C

    def test_no_circuits_returns_empty(self):
        with patch("handler._get_circuits_for_shift", return_value=[]):
            response = lambda_handler({"shift": "morning"}, MagicMock(aws_request_id="t"))
        assert response["statusCode"] == 200
        assert response["message"] == "no_active_circuits"
        assert response["results"] == []


# ─────────────────────────────────────────────────────────
# Tests: dataclass sanity
# ─────────────────────────────────────────────────────────

class TestCircuitGroup:

    def test_default_init(self):
        g = CircuitGroup(group_id="g1", depot_id="felipe_cardoso")
        assert g.circuit_ids == []
        assert g.total_containers == 0
        assert g.total_mandatory == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
