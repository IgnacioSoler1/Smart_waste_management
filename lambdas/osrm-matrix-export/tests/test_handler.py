"""Tests de osrm-matrix-export con OSRM y S3 mockeados."""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "cuopt-client"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("OUTPUT_BUCKET", "test-bucket")

import handler  # noqa: E402

DEPOTS = {
    "depot_felipe_cardoso": {"latitude": -34.8347, "longitude": -56.0967},
    "depot_estacion_transferencia": {"latitude": -34.8128, "longitude": -56.2645},
}


def _c(cid: str, circuit: str, seq: int, depot: str, shift: str) -> dict:
    return {"container_id": cid, "circuit_id": circuit, "csv_sequence": seq,
            "latitude": -34.9, "longitude": -56.1, "depot_id": depot,
            "shift": shift, "status": "active"}


def test_build_pair_nodes_filters_and_orders():
    containers = [
        _c("b1", "B", 1, "depot_felipe_cardoso", "night"),
        _c("a2", "A", 2, "depot_felipe_cardoso", "night"),
        _c("a0", "A", 0, "depot_felipe_cardoso", "night"),
        _c("x", "A", 3, "depot_felipe_cardoso", "morning"),
        _c("w", "W", 0, "depot_estacion_transferencia", "night"),
    ]
    nodes = handler.build_pair_nodes(containers, DEPOTS, "depot_felipe_cardoso", "night")
    assert nodes["node_ids"] == [
        "depot_estacion_transferencia", "depot_felipe_cardoso", "a0", "a2", "b1",
    ]
    assert nodes["depot_indices"] == {"depot_estacion_transferencia": 0, "depot_felipe_cardoso": 1}


def test_export_pair_writes_float32_matrices():
    containers = [_c(f"c{i}", "A", i, "depot_felipe_cardoso", "night") for i in range(3)]
    n = 5
    osrm = MagicMock()
    osrm.get_road_bearings.return_value = [None] * n
    osrm.get_distance_matrix.return_value = {
        "durations": [[float(i * n + j) for j in range(n)] for i in range(n)],
        "distances": [[None if (i, j) == (0, 1) else 1.0 for j in range(n)] for i in range(n)],
    }
    s3 = MagicMock()

    out = handler.export_pair(osrm, s3, containers, DEPOTS, "depot_felipe_cardoso", "night", True)

    assert out["n"] == n and out["unroutable_pairs"] == 1
    puts = {c.kwargs["Key"].rsplit("/", 1)[1]: c.kwargs["Body"] for c in s3.put_object.call_args_list}
    durations = struct.unpack(f"<{n * n}f", puts["durations.f32"])
    assert durations[n * 2 + 3] == 13.0
    distances = struct.unpack(f"<{n * n}f", puts["distances.f32"])
    assert distances[1] == float("inf")
    meta = json.loads(puts["meta.json"])
    assert meta["n"] == n and meta["node_ids"][2:] == ["c0", "c1", "c2"]
