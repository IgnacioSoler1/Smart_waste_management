"""
osrm-matrix-export — SmartWaste MVD

Lambda de invocación manual que calcula, con el OSRM de producción (ECS
Fargate, perfil de camión), las matrices "maestras" de duración y distancia
usadas por los experimentos offline de la revisión del paper
(paper/review-response/experiments/).

Una matriz por par (depósito, turno): nodos = [ambos depósitos] + todos los
contenedores activos de ese par, ordenados por (circuit_id, csv_sequence).
Cualquier subproblema de los experimentos (un circuito, un grupo consolidado)
es un sub-índice de estas matrices, así que todos los brazos comparan costos
sobre exactamente la misma red vial que el pipeline.

Los bearings se calculan igual que en el route-optimizer
(OSRMClient.get_road_bearings), para que los costos coincidan con producción.

Salida en s3://{OUTPUT_BUCKET}/{OUTPUT_PREFIX}/{pair}/:
    durations.f32   float32 little-endian, row-major, N×N (segundos)
    distances.f32   float32 little-endian, row-major, N×N (metros)
    meta.json       ids de nodo, depósitos, shape, bearings, timestamp

Evento (todos los campos opcionales):
    {"pairs": ["depot_felipe_cardoso|night", ...],   # default: los 6 pares
     "use_bearings": true}
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3

from osrm_client import OSRMClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_OSRM_URL      = os.environ.get("OSRM_URL", "http://localhost:5000")
_OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "")
_OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "experiments/osrm-matrices")
# Una tabla de ~2 800 nodos tarda del orden de 1 minuto en Fargate (2 vCPU).
_OSRM_TIMEOUT_S = float(os.environ.get("OSRM_TIMEOUT_S", "600"))

_DATA_DIR = Path(__file__).resolve().parent / "data"
_SHIFTS = ("morning", "afternoon", "night")


def _load_inputs() -> tuple[list[dict], dict[str, dict]]:
    containers = json.loads((_DATA_DIR / "containers_enriched.json").read_text())
    depots = json.loads((_DATA_DIR / "fixed_locations.json").read_text())
    return containers, depots


def build_pair_nodes(
    containers: list[dict],
    depots: dict[str, dict],
    depot_id: str,
    shift: str,
) -> dict[str, Any]:
    """
    Nodos de la matriz maestra de un par (depósito, turno).

    Returns:
        {"node_ids": [...], "locations": [(lat, lon), ...], "depot_indices": {id: idx}}
        Los depósitos ocupan los primeros índices, en orden de fixed_locations.json.
    """
    depot_ids = sorted(depots)
    selected = sorted(
        (
            c for c in containers
            if c.get("status", "active") == "active"
            and c.get("depot_id") == depot_id
            and c.get("shift") == shift
        ),
        key=lambda c: (str(c["circuit_id"]), int(c.get("csv_sequence", 0))),
    )
    node_ids = depot_ids + [str(c["container_id"]) for c in selected]
    locations = [(float(depots[d]["latitude"]), float(depots[d]["longitude"])) for d in depot_ids]
    locations += [(float(c["latitude"]), float(c["longitude"])) for c in selected]
    return {
        "node_ids": node_ids,
        "locations": locations,
        "depot_indices": {d: i for i, d in enumerate(depot_ids)},
    }


def _to_f32_bytes(matrix: list[list[float | None]]) -> bytes:
    # OSRM devuelve null para pares no ruteables; se codifican como +inf.
    buf = array("f")
    for row in matrix:
        buf.extend(float("inf") if v is None else float(v) for v in row)
    if sys.byteorder != "little":
        buf.byteswap()
    return buf.tobytes()


def export_pair(
    osrm: OSRMClient,
    s3: Any,
    containers: list[dict],
    depots: dict[str, dict],
    depot_id: str,
    shift: str,
    use_bearings: bool,
) -> dict[str, Any]:
    pair = f"{depot_id}|{shift}"
    nodes = build_pair_nodes(containers, depots, depot_id, shift)
    n = len(nodes["locations"])
    logger.info("[%s] %d nodos", pair, n)

    t0 = time.monotonic()
    bearings = None
    if use_bearings:
        bearings = osrm.get_road_bearings(
            nodes["locations"], depot_indices=set(nodes["depot_indices"].values()),
        )
    t_bearings = time.monotonic() - t0

    result = osrm.get_distance_matrix(nodes["locations"], bearings=bearings)
    t_table = time.monotonic() - t0 - t_bearings
    logger.info("[%s] bearings %.1f s, table %.1f s", pair, t_bearings, t_table)

    unroutable = sum(
        1
        for drow, xrow in zip(result["durations"], result["distances"])
        for d, x in zip(drow, xrow)
        if d is None or x is None
    )
    key_prefix = f"{_OUTPUT_PREFIX}/{depot_id}__{shift}"
    meta = {
        "pair": pair,
        "depot_id": depot_id,
        "shift": shift,
        "n": n,
        "dtype": "float32-le",
        "layout": "row-major",
        "node_ids": nodes["node_ids"],
        "depot_indices": nodes["depot_indices"],
        "bearings": bearings,
        "unroutable_pairs": unroutable,
        "osrm_url": _OSRM_URL,
        "bearings_seconds": round(t_bearings, 1),
        "table_seconds": round(t_table, 1),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    s3.put_object(Bucket=_OUTPUT_BUCKET, Key=f"{key_prefix}/durations.f32",
                  Body=_to_f32_bytes(result["durations"]))
    s3.put_object(Bucket=_OUTPUT_BUCKET, Key=f"{key_prefix}/distances.f32",
                  Body=_to_f32_bytes(result["distances"]))
    s3.put_object(Bucket=_OUTPUT_BUCKET, Key=f"{key_prefix}/meta.json",
                  Body=json.dumps(meta).encode(), ContentType="application/json")
    return {"pair": pair, "n": n, "unroutable_pairs": unroutable,
            "bearings_s": meta["bearings_seconds"], "table_s": meta["table_seconds"],
            "s3_prefix": f"s3://{_OUTPUT_BUCKET}/{key_prefix}/"}


def lambda_handler(event: dict, context) -> dict:
    if not _OUTPUT_BUCKET:
        return {"statusCode": 500, "error": "OUTPUT_BUCKET not configured"}

    containers, depots = _load_inputs()
    requested = event.get("pairs") or [f"{d}|{s}" for d in sorted(depots) for s in _SHIFTS]
    use_bearings = bool(event.get("use_bearings", True))

    osrm = OSRMClient(base_url=_OSRM_URL, timeout=_OSRM_TIMEOUT_S)
    s3 = boto3.client("s3")

    results, errors = [], []
    for pair in requested:
        depot_id, shift = pair.split("|")
        try:
            results.append(export_pair(osrm, s3, containers, depots, depot_id, shift, use_bearings))
        except Exception as exc:  # seguir con el resto de los pares
            logger.error("[%s] fallo: %s", pair, exc, exc_info=True)
            errors.append({"pair": pair, "error": str(exc)})

    return {"statusCode": 200 if not errors else 207, "exported": results, "errors": errors}
