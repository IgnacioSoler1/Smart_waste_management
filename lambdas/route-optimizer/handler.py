"""
handler.py — SmartWaste MVD / route-optimizer

Optimiza rutas de recolección por circuito usando OSRM (distance matrix)
+ cuOpt o OR-Tools (VRP solver).

Flujo por circuito:
  1. Leer contenedores con needs_collection=True del GSI circuit-index
  2. Si < MIN_CONTAINERS: skip
  3. Leer camiones con status="idle" para el circuito
  4. Construir locations: [depot] + [containers] + [depot]
  5. OSRM Table API → matriz de duraciones N×N
  6. VRP solver → rutas óptimas
  7. Marcar rutas previas como "superseded", guardar nuevas en DynamoDB
  8. Retornar resumen

Invocación vía EventBridge (cada 15 min):
  {}                          → detecta turno actual, optimiza todos sus circuitos
  {"shift": "morning"}        → todos los circuitos del turno mañana
  {"circuit_id": "A_DU_..."}  → solo ese circuito

Environment vars (inyectadas por Terraform):
  CONTAINERS_TABLE, TRUCKS_TABLE, ROUTES_TABLE
  OSRM_URL          (default: http://localhost:5000)
  CUOPT_MODE        (ortools | api_catalog | self_hosted; default: ortools)
  CUOPT_API_KEY     (solo para api_catalog)
  CUOPT_SERVER_URL  (solo para self_hosted)
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

# osrm_client, vrp_solver y constraints se copian al directorio del Lambda
# durante el build (build.sh), por eso son importaciones locales.
from osrm_client import OSRMClient, OSRMError
from vrp_solver import CuOptSolver, ORToolsSolver
from constraints import calculate_prize, estimate_demand_kg

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────

_CONTAINERS_TABLE  = os.environ["CONTAINERS_TABLE"]
_TRUCKS_TABLE      = os.environ["TRUCKS_TABLE"]
_ROUTES_TABLE      = os.environ["ROUTES_TABLE"]
_OSRM_URL          = os.environ.get("OSRM_URL", "http://localhost:5000")
_CUOPT_MODE        = os.environ.get("CUOPT_MODE", "ortools")
_CUOPT_API_KEY     = os.environ.get("CUOPT_API_KEY")
_CUOPT_SERVER_URL  = os.environ.get("CUOPT_SERVER_URL")
_ROUTE_RESULTS_FIREHOSE = os.environ.get("ROUTE_RESULTS_FIREHOSE", "")

# Número mínimo de contenedores para que valga la pena optimizar el circuito.
# Con menos de 5 paradas la ruta trivial (en orden) es casi óptima.
_MIN_CONTAINERS = 5

# Capacidad de camión por defecto (kg). Los camiones de MVD llevan ~25 t.
_DEFAULT_TRUCK_CAPACITY_KG = 25_000.0

# ─────────────────────────────────────────────────────────
# Consolidated multi-circuit solve (Vector 5.1 de docs/cuopt.md)
# ─────────────────────────────────────────────────────────

# Tamaño máximo de matriz OSRM por llamada (locations).
# El cliente OSRM tiene un cap de 5000 (osrm_client._MAX_TABLE_SIZE),
# y el server ECS soporta hasta 10000. El verdadero binding es cuOpt:
#   - cuOpt cloud API:  ~1,000 nodos por solve (límite documentado)
#   - cuOpt self-hosted: ~10,000+ nodos
# Default 1000 para aprovechar al máximo el cloud sin pasar el límite;
# subir a 5000+ requiere cuopt_mode=self_hosted.
_MAX_LOCATIONS_PER_GROUP = int(os.environ.get("MAX_LOCATIONS_PER_GROUP", "1000"))

# Cantidad mínima de circuitos en un grupo para activar la consolidación.
# Con 1 circuito hay que caer al path single-circuit original (no hay nada que consolidar).
_MIN_CIRCUITS_FOR_CONSOLIDATION = int(os.environ.get("MIN_CIRCUITS_FOR_CONSOLIDATION", "2"))

# Depósitos de Montevideo (lat, lon)
_DEPOTS: dict[str, tuple[float, float]] = {
    "felipe_cardoso": (-34.8347, -56.0967),  # noreste — zona este
    "ruta_102":       (-34.8128, -56.2645),  # oeste   — zona oeste
}
_DEFAULT_DEPOT_ID = "felipe_cardoso"

# Alias de depot_id tal como aparecen en data/processed/*.json y en DynamoDB.
# Antes de este mapeo, los ids reales ("depot_felipe_cardoso",
# "depot_estacion_transferencia") no matcheaban _DEPOTS y todos los circuitos
# caían al depósito por defecto.
_DEPOT_ALIASES: dict[str, str] = {
    "felipe_cardoso":               "felipe_cardoso",
    "depot_felipe_cardoso":         "felipe_cardoso",
    "ruta_102":                     "ruta_102",
    "depot_estacion_transferencia": "ruta_102",
    "estacion_transferencia":       "ruta_102",
}

# Contenedores con fill <= este umbral no entran al solver (prize despreciable),
# pero sí cuentan para el baseline estático, que visita todos los activos.
_CANDIDATE_MIN_FILL = 20.0

# ─────────────────────────────────────────────────────────
# Clientes AWS (reutilizados entre invocaciones)
# ─────────────────────────────────────────────────────────

_dynamodb        = boto3.resource("dynamodb")
_tbl_containers  = _dynamodb.Table(_CONTAINERS_TABLE)
_tbl_trucks      = _dynamodb.Table(_TRUCKS_TABLE)
_tbl_routes      = _dynamodb.Table(_ROUTES_TABLE)
_osrm            = OSRMClient(base_url=_OSRM_URL)
_firehose        = boto3.client("firehose") if _ROUTE_RESULTS_FIREHOSE else None


# ─────────────────────────────────────────────────────────
# Helpers: turno y circuitos activos
# ─────────────────────────────────────────────────────────

def _current_shift() -> str:
    """Detecta el turno activo según la hora actual en Montevideo (UTC-3)."""
    now_mvd = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-3)))
    h = now_mvd.hour
    if 6 <= h < 14:
        return "morning"
    if 14 <= h < 22:
        return "afternoon"
    return "night"


def _get_circuits_for_shift(shift: str) -> list[str]:
    """
    Escanea la tabla de contenedores filtrando por turno + needs_collection=True
    y retorna los circuit_ids únicos.

    Un Scan de ~11 000 ítems a 300 bytes ≈ 3 MB — aceptable cada 15 min.
    """
    circuit_ids: set[str] = set()
    kwargs: dict[str, Any] = {
        "FilterExpression": (
            Attr("needs_collection").eq(True)
            & Attr("shift").eq(shift)
            & Attr("status").eq("active")
        ),
        "ProjectionExpression": "circuit_id",
    }
    while True:
        resp = _tbl_containers.scan(**kwargs)
        for item in resp.get("Items", []):
            if "circuit_id" in item:
                circuit_ids.add(str(item["circuit_id"]))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    logger.info("Turno '%s': %d circuitos con contenedores pendientes", shift, len(circuit_ids))
    return sorted(circuit_ids)


# ─────────────────────────────────────────────────────────
# Helpers: lectura de DynamoDB
# ─────────────────────────────────────────────────────────

def _resolve_depot(container: dict) -> str:
    """
    Devuelve la clave canónica de _DEPOTS para un contenedor.

    Usa depot_id (con alias) si existe; si no, el depósito conocido más cercano
    a depot_lat/depot_lon (los ítems sembrados por seed_dynamodb.py antes del
    fix solo traen coordenadas). Cae a _DEFAULT_DEPOT_ID si no hay nada.
    """
    raw_id = container.get("depot_id")
    if raw_id is not None:
        canonical = _DEPOT_ALIASES.get(str(raw_id))
        if canonical:
            return canonical
    lat, lon = container.get("depot_lat"), container.get("depot_lon")
    if lat is not None and lon is not None:
        lat_f, lon_f = float(lat), float(lon)
        if lat_f != 0.0 or lon_f != 0.0:
            return min(
                _DEPOTS,
                key=lambda k: (_DEPOTS[k][0] - lat_f) ** 2 + (_DEPOTS[k][1] - lon_f) ** 2,
            )
    return _DEFAULT_DEPOT_ID


def _is_candidate(container: dict) -> bool:
    """True si el contenedor entra al solver (fill > _CANDIDATE_MIN_FILL)."""
    return float(container.get("fill_level", 0)) > _CANDIDATE_MIN_FILL


def _candidate_nodes(all_containers: list[dict]) -> list[int]:
    """
    Índices de nodo (en la matriz construida sobre all_containers con
    _build_problem) que forman el subproblema del solver:
    [depot_start] + candidatos + [depot_end].
    """
    n = len(all_containers) + 2
    return [0] + [i + 1 for i, c in enumerate(all_containers) if _is_candidate(c)] + [n - 1]


def _submatrix(matrix: list[list], nodes: list[int]) -> list[list]:
    return [[matrix[a][b] for b in nodes] for a in nodes]


def _get_containers(circuit_id: str) -> list[dict]:
    """
    Retorna TODOS los contenedores activos del circuito.

    El baseline estático los visita a todos; el solver solo recibe los
    candidatos (fill > _CANDIDATE_MIN_FILL, ver _is_candidate). Los de
    20-60% entran como opcionales con prize.
    """
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "IndexName": "circuit-index",
        "KeyConditionExpression": Key("circuit_id").eq(circuit_id),
        "FilterExpression": Attr("status").eq("active"),
    }
    while True:
        resp = _tbl_containers.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def _get_idle_trucks(circuit_id: str) -> list[dict]:
    """Retorna camiones con status='idle' asignados al circuito."""
    resp = _tbl_trucks.query(
        IndexName="status-index",
        KeyConditionExpression=Key("status").eq("idle"),
        FilterExpression=Attr("circuit_id").eq(circuit_id),
    )
    return resp.get("Items", [])


# ─────────────────────────────────────────────────────────
# Helpers: solver y locations
# ─────────────────────────────────────────────────────────

def _make_solver() -> ORToolsSolver | CuOptSolver:
    if _CUOPT_MODE in ("api_catalog", "self_hosted"):
        return CuOptSolver(
            mode=_CUOPT_MODE,
            api_key=_CUOPT_API_KEY,
            server_url=_CUOPT_SERVER_URL,
        )
    if _CUOPT_MODE != "ortools":
        logger.warning("CUOPT_MODE=%r desconocido, usando ortools", _CUOPT_MODE)
    return ORToolsSolver()


def _build_problem(
    containers: list[dict],
    depot_coords: tuple[float, float],
) -> tuple[list[tuple[float, float]], list[float], list[float], int, int]:
    """
    Devuelve (locations, demands, prizes, depot_start_idx, depot_end_idx).

    locations[0] = locations[-1] = depot; [1..n-1] = contenedores.
    demands[0]   = demands[-1]   = 0.0;   [1..n-1] = kg estimados.
    prizes[0]    = prizes[-1]    = 0.0;   [1..n-1] = prize por fill_level.
    """
    container_locs = [(float(c["latitude"]), float(c["longitude"])) for c in containers]
    container_demands = [
        estimate_demand_kg(float(c.get("fill_level", 50)), float(c.get("capacity_liters", 2400)))
        for c in containers
    ]
    container_prizes = [
        calculate_prize(float(c.get("fill_level", 50)))
        for c in containers
    ]
    locations = [depot_coords] + container_locs + [depot_coords]
    demands   = [0.0]          + container_demands + [0.0]
    prizes    = [0.0]          + container_prizes  + [0.0]
    return locations, demands, prizes, 0, len(locations) - 1


# ─────────────────────────────────────────────────────────
# Helpers: baseline (ruta original CSV)
# ─────────────────────────────────────────────────────────

def _compute_baseline_csv_order(
    containers: list[dict],
    durations_matrix: list[list[int]],
    distances_matrix: list[list[float]],
    depot_start_idx: int,
    depot_end_idx: int,
) -> dict:
    """
    Calcula la ruta baseline visitando TODOS los contenedores en el orden
    original del CSV de Intendencia (campo csv_sequence).

    Esto simula la ruta estatica donde el camion visita cada contenedor
    sin importar fill_level, en el orden historico asignado.
    """
    sorted_containers = sorted(
        enumerate(containers),
        key=lambda x: int(x[1].get("csv_sequence", 0)),
    )

    # Tour: depot_start -> containers en orden CSV -> depot_end
    tour_node_indices = [depot_start_idx]
    for orig_idx, _ in sorted_containers:
        tour_node_indices.append(orig_idx + 1)  # +1 por depot_start en pos 0
    tour_node_indices.append(depot_end_idx)

    total_duration_s = sum(
        durations_matrix[tour_node_indices[i]][tour_node_indices[i + 1]]
        for i in range(len(tour_node_indices) - 1)
    )
    total_distance_m = sum(
        distances_matrix[tour_node_indices[i]][tour_node_indices[i + 1]]
        for i in range(len(tour_node_indices) - 1)
    )

    return {
        "baseline_distance_m": total_distance_m,
        "baseline_duration_s": total_duration_s,
        "baseline_stops": len(containers),
    }


# ─────────────────────────────────────────────────────────
# Helpers: escritura en DynamoDB
# ─────────────────────────────────────────────────────────

def _supersede_routes(circuit_id: str) -> None:
    """
    Marca como 'superseded' todas las rutas activas del circuito.

    Usa Query sobre el GSI circuit-index (O(rutas del circuito), típicamente 1-4 items)
    en lugar de Scan de toda la tabla. El FilterExpression adicional por status='active'
    evita actualizar las ya superseded (que siguen en el índice hasta que expira su TTL).

    Las rutas superseded reciben un TTL de 24 horas para limpieza automática.
    El historial a largo plazo se conserva en S3 (Firehose → Parquet).
    """
    import time as _time
    expires_at = int(_time.time()) + 24 * 3600  # 24 horas en epoch seconds

    kwargs: dict = {
        "IndexName": "circuit-index",
        "KeyConditionExpression": Key("circuit_id").eq(circuit_id),
        "FilterExpression": Attr("status").eq("active"),
        "ProjectionExpression": "route_id",
    }
    while True:
        resp = _tbl_routes.query(**kwargs)
        for item in resp.get("Items", []):
            try:
                _tbl_routes.update_item(
                    Key={"route_id": item["route_id"]},
                    UpdateExpression="SET #s = :sup, expires_at = :ttl",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":sup": "superseded",
                        ":ttl": expires_at,
                    },
                    ConditionExpression=Attr("status").eq("active"),
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _save_route(
    truck_id: str,
    circuit_id: str,
    route_node_indices: list[int],
    containers: list[dict],
    depot_coords: tuple[float, float],
    durations_matrix: list[list[int]],
    distances_matrix: list[list[float]],
    vrp_result: dict,
    bearings: list[tuple[int, int] | None] | None = None,
    baseline: dict | None = None,
    group_id: str | None = None,
    origin_circuit_ids: list[str] | None = None,
) -> tuple[str, float, float]:
    """
    Persiste la ruta en DynamoDB.

    Returns:
        (route_id, total_duration_s, total_distance_m)
    """
    route_id = str(uuid.uuid4())
    now_iso  = datetime.now(timezone.utc).isoformat()
    n        = len(containers) + 2  # depot_start + containers + depot_end

    # Calcular distancia y duración sumando arcos consecutivos
    total_duration_s: float = sum(
        durations_matrix[route_node_indices[i]][route_node_indices[i + 1]]
        for i in range(len(route_node_indices) - 1)
    )
    total_distance_m: float = sum(
        distances_matrix[route_node_indices[i]][route_node_indices[i + 1]]
        for i in range(len(route_node_indices) - 1)
    )

    # Construir lista de paradas (excluir nodos de depósito: 0 y n-1)
    stops: list[dict] = []
    depot_indices = {0, n - 1}
    for seq, node_idx in enumerate(route_node_indices):
        if node_idx in depot_indices:
            continue
        c = containers[node_idx - 1]   # offset -1 porque el depósito ocupa [0]
        stops.append({
            "sequence":     seq,
            "container_id": c["container_id"],
            "latitude":     c["latitude"],
            "longitude":    c["longitude"],
            "fill_level":   c.get("fill_level", Decimal("0")),
            "demand_kg":    Decimal(str(round(
                estimate_demand_kg(
                    float(c.get("fill_level", 50)),
                    float(c.get("capacity_liters", 2400)),
                ), 2
            ))),
        })

    # Obtener geometría real de calles de OSRM para la ruta ordenada
    route_geometry: list[list[Decimal]] | None = None
    try:
        waypoints = []
        route_bearings: list[tuple[int, int] | None] | None = None
        if bearings is not None:
            route_bearings = []
        for node_idx in route_node_indices:
            if node_idx in depot_indices:
                waypoints.append(depot_coords)
                if route_bearings is not None:
                    route_bearings.append(None)
            else:
                c = containers[node_idx - 1]
                waypoints.append((float(c["latitude"]), float(c["longitude"])))
                if route_bearings is not None:
                    route_bearings.append(bearings[node_idx])
        geo_result = _osrm.get_route(waypoints, bearings=route_bearings)
        # GeoJSON coordinates son [lon, lat] — convertir a [lat, lon]
        route_geometry = [
            [Decimal(str(round(lat, 6))), Decimal(str(round(lon, 6)))]
            for lon, lat in geo_result["geometry"]["coordinates"]
        ]
        logger.info(
            "[circuit=%s] Geometría OSRM: %d puntos",
            circuit_id, len(route_geometry),
        )
    except Exception as geo_exc:
        logger.warning(
            "[circuit=%s] No se pudo obtener geometría OSRM: %s", circuit_id, geo_exc
        )

    item: dict = {
        "route_id":        route_id,
        "truck_id":        truck_id,
        "circuit_id":      circuit_id,
        "status":          "active",
        "created_at":      now_iso,
        "stops":           stops,
        "total_distance_m": Decimal(str(round(total_distance_m))),
        "total_duration_s": Decimal(str(round(total_duration_s))),
        "solver":          vrp_result["solver"],
        "solver_status":   vrp_result["status"],
        "depot_lat":       Decimal(str(depot_coords[0])),
        "depot_lon":       Decimal(str(depot_coords[1])),
    }
    if group_id is not None:
        item["group_id"] = group_id
    if origin_circuit_ids:
        # Set de circuitos originales que aportaron stops a esta ruta
        # (en modo single-circuit = [circuit_id]; en modo consolidado puede ser >1).
        item["origin_circuit_ids"] = sorted(set(origin_circuit_ids))
    if route_geometry is not None:
        item["route_geometry"] = route_geometry

    if baseline:
        item["baseline_distance_m"] = Decimal(str(round(baseline["baseline_distance_m"])))
        item["baseline_duration_s"] = Decimal(str(round(baseline["baseline_duration_s"])))
        item["baseline_stops"] = baseline["baseline_stops"]
        if baseline["baseline_distance_m"] > 0:
            item["distance_improvement_pct"] = Decimal(str(round(
                (1 - total_distance_m / baseline["baseline_distance_m"]) * 100, 1
            )))
        if baseline["baseline_duration_s"] > 0:
            item["duration_improvement_pct"] = Decimal(str(round(
                (1 - total_duration_s / baseline["baseline_duration_s"]) * 100, 1
            )))

    _tbl_routes.put_item(Item=item)

    # Publicar resumen de ruta a Kinesis Firehose para analytics históricos.
    # Firehose bufferiza los registros y produce archivos GZIP más grandes en S3.
    # Solo campos escalares — el array stops[] se queda en DynamoDB.
    if _firehose and _ROUTE_RESULTS_FIREHOSE:
        date_str = now_iso[:10]  # YYYY-MM-DD
        route_summary_s3 = {
            "date":                     date_str,
            "circuit_id":               circuit_id,
            "route_id":                 route_id,
            "truck_id":                 truck_id,
            "group_id":                 group_id or "",
            "origin_circuit_ids":       sorted(set(origin_circuit_ids)) if origin_circuit_ids else [circuit_id],
            "consolidated":             bool(group_id),
            "created_at":               now_iso,
            "baseline_distance_m":      round(baseline["baseline_distance_m"])   if baseline else 0,
            "total_distance_m":         round(total_distance_m),
            "baseline_duration_s":      round(baseline["baseline_duration_s"])   if baseline else 0,
            "total_duration_s":         round(total_duration_s),
            "baseline_stops":           baseline["baseline_stops"]               if baseline else 0,
            "optimized_stops":          len(stops),
            "stops_skipped":            max(0, (baseline["baseline_stops"] if baseline else 0) - len(stops)),
            "distance_improvement_pct": float(item.get("distance_improvement_pct", 0)),
            "duration_improvement_pct": float(item.get("duration_improvement_pct", 0)),
            "solver":                   vrp_result.get("solver", ""),
            "solver_status":            vrp_result.get("status", ""),
        }
        # Firehose espera registros NDJSON (una línea por registro).
        # El salto de línea al final es el delimitador entre registros en el archivo S3.
        record_data = (json.dumps(route_summary_s3, ensure_ascii=False) + "\n").encode()
        try:
            _firehose.put_record(
                DeliveryStreamName=_ROUTE_RESULTS_FIREHOSE,
                Record={"Data": record_data},
            )
            logger.info("Ruta publicada a Firehose: %s (stream=%s)", route_id, _ROUTE_RESULTS_FIREHOSE)
        except Exception as exc:
            logger.warning("No se pudo publicar ruta a Firehose (%s): %s", route_id, exc)

    # Escribir el puntero directo en la tabla trucks para que el driver
    # app pueda resolver su ruta con un solo GET /trucks/{id} + GET /routes/{id},
    # sin necesitar un GSI en la tabla routes.
    if not truck_id.startswith("virtual-"):
        try:
            _tbl_trucks.update_item(
                Key={"truck_id": truck_id},
                UpdateExpression="SET active_route_id = :rid",
                ExpressionAttributeValues={":rid": route_id},
            )
        except Exception as exc:
            logger.warning("No se pudo actualizar active_route_id en truck %s: %s", truck_id, exc)

    return route_id, total_duration_s, total_distance_m


# ─────────────────────────────────────────────────────────
# Helpers: grouping multi-circuito (consolidation)
# ─────────────────────────────────────────────────────────

@dataclass
class CircuitGroup:
    """
    Conjunto de circuitos que comparten depósito y se resuelven en un único
    solve consolidado de cuOpt/OR-Tools. Cuando el grupo tiene un solo circuito
    se cae al path single-circuit original (sin overhead de consolidación).
    """
    group_id: str
    depot_id: str
    circuit_ids: list[str] = field(default_factory=list)
    total_containers: int = 0
    total_mandatory: int = 0


def _prefetch_circuit_data(circuit_ids: list[str]) -> dict[str, dict]:
    """
    Para cada circuit_id, hace una query a containers + trucks y guarda los
    resultados en memoria. Esto permite agrupar antes de optimizar sin
    duplicar queries cuando un grupo cae al path single-circuit.

    Returns:
        dict mapeando circuit_id → {
            "containers":  list[dict],   # todos los activos (baseline + matriz)
            "trucks":      list[dict],
            "depot_id":    str,
            "n_total":     int,          # contenedores activos (tamaño en la matriz)
            "n_candidates": int,         # contenedores con fill > 20% (van al solver)
            "n_mandatory": int,          # contenedores con fill > 60%
        }
    """
    data: dict[str, dict] = {}
    for cid in circuit_ids:
        try:
            containers = _get_containers(cid)
        except Exception as exc:
            logger.warning("[circuit=%s] Prefetch containers fallo: %s", cid, exc)
            containers = []
        try:
            trucks = _get_idle_trucks(cid)
        except Exception as exc:
            logger.warning("[circuit=%s] Prefetch trucks fallo: %s", cid, exc)
            trucks = []

        depot_id = _resolve_depot(containers[0]) if containers else _DEFAULT_DEPOT_ID
        n_mandatory = sum(1 for c in containers if float(c.get("fill_level", 0)) > 60)

        data[cid] = {
            "containers":   containers,
            "trucks":       trucks,
            "depot_id":     depot_id,
            "n_total":      len(containers),
            "n_candidates": sum(1 for c in containers if _is_candidate(c)),
            "n_mandatory":  n_mandatory,
        }
    return data


def _group_circuits_by_depot_shift(
    circuit_data: dict[str, dict],
    shift: str,
    max_locations_per_group: int = _MAX_LOCATIONS_PER_GROUP,
) -> list[CircuitGroup]:
    """
    Agrupa circuitos por depósito (felipe_cardoso vs ruta_102) en bins que
    respetan el límite de OSRM (≤500 locations). Greedy bin-packing: ordena
    los circuitos por tamaño descendente y los va llenando.

    Reserva 2 slots para los nodos de depósito (start + end), así que el cap
    efectivo de contenedores por grupo es max_locations_per_group - 2.
    """
    # Reservar 2 para depot start + end
    container_cap = max_locations_per_group - 2

    # Bin por depósito
    by_depot: dict[str, list[tuple[str, int]]] = {}
    for cid, d in circuit_data.items():
        by_depot.setdefault(d["depot_id"], []).append((cid, d["n_total"]))

    groups: list[CircuitGroup] = []
    for depot_id, circuits in by_depot.items():
        # Greedy: ordenar por tamaño descendente
        circuits_sorted = sorted(circuits, key=lambda x: -x[1])

        current = CircuitGroup(
            group_id=f"{shift}-{depot_id}-0",
            depot_id=depot_id,
        )
        group_idx = 0

        for cid, n in circuits_sorted:
            # Un circuito individual que supera el cap: lo dejamos solo en su grupo
            # (caerá al fallback single-circuit, lo cual respeta su propio tamaño).
            if n > container_cap:
                if current.circuit_ids:
                    groups.append(current)
                    group_idx += 1
                solo = CircuitGroup(
                    group_id=f"{shift}-{depot_id}-{group_idx}",
                    depot_id=depot_id,
                )
                solo.circuit_ids.append(cid)
                solo.total_containers = n
                solo.total_mandatory = circuit_data[cid]["n_mandatory"]
                groups.append(solo)
                group_idx += 1
                current = CircuitGroup(
                    group_id=f"{shift}-{depot_id}-{group_idx}",
                    depot_id=depot_id,
                )
                continue

            # ¿Cabe en el grupo actual?
            if current.total_containers + n > container_cap and current.circuit_ids:
                # Cerrar grupo actual, abrir uno nuevo
                groups.append(current)
                group_idx += 1
                current = CircuitGroup(
                    group_id=f"{shift}-{depot_id}-{group_idx}",
                    depot_id=depot_id,
                )

            current.circuit_ids.append(cid)
            current.total_containers += n
            current.total_mandatory += circuit_data[cid]["n_mandatory"]

        if current.circuit_ids:
            groups.append(current)

    logger.info(
        "Grouping %s: %d circuits → %d groups (sizes: %s)",
        shift, len(circuit_data), len(groups),
        [(g.group_id, len(g.circuit_ids), g.total_containers) for g in groups],
    )
    return groups


def _baseline_for_origin_circuit(
    containers_global: list[dict],
    origin_circuit_id: str,
    durations_matrix: list[list[int]],
    distances_matrix: list[list[float]],
    depot_start_idx: int,
    depot_end_idx: int,
) -> dict:
    """
    Variante de _compute_baseline_csv_order para el modo consolidado.

    En el problema consolidado los containers de varios circuitos están en
    el mismo array (containers_global). Calculamos el baseline (ruta secuencial
    CSV) solo para los containers de un origin_circuit_id puntual, usando los
    índices globales en la matriz consolidada para que sea comparable con la
    ruta optimizada que también referencia esa misma matriz.
    """
    # Filtrar containers de este circuito + recordar índice global (+1 por depot)
    filtered = [
        (global_idx + 1, c)  # +1 porque depot_start ocupa idx 0
        for global_idx, c in enumerate(containers_global)
        if str(c.get("circuit_id", "")) == origin_circuit_id
    ]
    if not filtered:
        return {
            "baseline_distance_m": 0.0,
            "baseline_duration_s": 0.0,
            "baseline_stops": 0,
        }

    # Ordenar por csv_sequence
    filtered.sort(key=lambda x: int(x[1].get("csv_sequence", 0)))

    tour = [depot_start_idx] + [idx for idx, _ in filtered] + [depot_end_idx]

    total_duration_s = sum(
        durations_matrix[tour[i]][tour[i + 1]] for i in range(len(tour) - 1)
    )
    total_distance_m = sum(
        distances_matrix[tour[i]][tour[i + 1]] for i in range(len(tour) - 1)
    )
    return {
        "baseline_distance_m": total_distance_m,
        "baseline_duration_s": total_duration_s,
        "baseline_stops": len(filtered),
    }


# ─────────────────────────────────────────────────────────
# Optimización de un circuito
# ─────────────────────────────────────────────────────────

def _optimize_circuit(circuit_id: str) -> dict:
    """
    Ejecuta el pipeline completo de optimización para un circuito.
    Nunca lanza excepciones — captura y retorna {"status": "error"}.
    """
    prefix = f"[circuit={circuit_id}]"

    # ── a. Contenedores pendientes ────────────────────────
    try:
        all_containers = _get_containers(circuit_id)
    except Exception as exc:
        logger.error("%s Error al leer contenedores: %s", prefix, exc, exc_info=True)
        return {"circuit_id": circuit_id, "status": "error", "error": f"dynamo: {exc}"}
    containers = [c for c in all_containers if _is_candidate(c)]

    n_mandatory = sum(1 for c in containers if float(c.get("fill_level", 0)) > 60)
    n_optional  = len(containers) - n_mandatory
    logger.info(
        "%s %d contenedores (obligatorios=%d, opcionales=%d)",
        prefix, len(containers), n_mandatory, n_optional,
    )

    # ── b. Skip si pocos contenedores obligatorios ─────────
    if n_mandatory < _MIN_CONTAINERS:
        logger.info("%s Saltando — %d contenedores obligatorios < mínimo %d",
                    prefix, n_mandatory, _MIN_CONTAINERS)
        return {
            "circuit_id": circuit_id,
            "status":     "skipped",
            "reason":     f"fewer_than_{_MIN_CONTAINERS}_containers",
            "containers": len(containers),
        }

    # ── c. Camiones disponibles ───────────────────────────
    try:
        trucks = _get_idle_trucks(circuit_id)
    except Exception as exc:
        logger.warning("%s Error al leer camiones, usando 1 virtual: %s", prefix, exc)
        trucks = []

    if trucks:
        num_vehicles = len(trucks)
        capacities   = [float(t.get("capacity_kg", _DEFAULT_TRUCK_CAPACITY_KG)) for t in trucks]
        truck_ids    = [t["truck_id"] for t in trucks]
    else:
        # Sin camiones reales: estimar cuántos se necesitan según demanda total
        total_demand = sum(
            estimate_demand_kg(float(c.get("fill_level", 50)), float(c.get("capacity_liters", 2400)))
            for c in containers
        )
        num_vehicles = max(1, math.ceil(total_demand / _DEFAULT_TRUCK_CAPACITY_KG))
        capacities   = [_DEFAULT_TRUCK_CAPACITY_KG] * num_vehicles
        truck_ids    = [f"virtual-{circuit_id}-{v}" for v in range(num_vehicles)]

    logger.info("%s %d vehículo(s): %s", prefix, num_vehicles, truck_ids)

    # ── d. Depot y locations ──────────────────────────────
    # La matriz OSRM cubre todos los activos (baseline); el solver usa la
    # submatriz de candidatos.
    depot_id     = _resolve_depot(all_containers[0])
    depot_coords = _DEPOTS[depot_id]
    all_locations, _, _, full_start_idx, full_end_idx = _build_problem(
        all_containers, depot_coords
    )
    _, demands, prizes, depot_start_idx, depot_end_idx = _build_problem(
        containers, depot_coords
    )
    nodes = _candidate_nodes(all_containers)

    # ── e. OSRM — bearings + matriz de distancias ─────────
    try:
        all_bearings = _osrm.get_road_bearings(
            all_locations, depot_indices={full_start_idx, full_end_idx},
        )
    except Exception as exc:
        logger.warning("%s No se pudieron obtener bearings, continuando sin ellos: %s", prefix, exc)
        all_bearings = None

    try:
        osrm_result = _osrm.get_distance_matrix(all_locations, bearings=all_bearings)
    except OSRMError as exc:
        logger.error("%s OSRM error: %s", prefix, exc)
        return {"circuit_id": circuit_id, "status": "error", "error": f"osrm: {exc}"}
    except Exception as exc:
        logger.error("%s OSRM falla inesperada: %s", prefix, exc, exc_info=True)
        return {"circuit_id": circuit_id, "status": "error", "error": f"osrm: {exc}"}

    full_cost_matrix = [[int(d) for d in row] for row in osrm_result["durations"]]

    # ── e2. Baseline: ruta estática (todos los activos, orden CSV) ──
    baseline = _compute_baseline_csv_order(
        all_containers, full_cost_matrix, osrm_result["distances"],
        full_start_idx, full_end_idx,
    )

    cost_matrix      = _submatrix(full_cost_matrix, nodes)
    distances_matrix = _submatrix(osrm_result["distances"], nodes)
    bearings         = [all_bearings[k] for k in nodes] if all_bearings is not None else None
    logger.info(
        "%s Baseline CSV: %.1f km, %.1f min, %d paradas",
        prefix,
        baseline["baseline_distance_m"] / 1000,
        baseline["baseline_duration_s"] / 60,
        baseline["baseline_stops"],
    )

    # ── f. VRP solver ─────────────────────────────────────
    try:
        solver     = _make_solver()
        vrp_result = solver.solve_vrp(
            cost_matrix     = cost_matrix,
            num_vehicles    = num_vehicles,
            demands         = demands,
            capacities      = capacities,
            depot_start_idx = depot_start_idx,
            depot_end_idx   = depot_end_idx,
            time_limit      = 5,
            prizes          = prizes,
        )
    except Exception as exc:
        logger.error("%s Solver error: %s", prefix, exc, exc_info=True)
        return {"circuit_id": circuit_id, "status": "error", "error": f"solver: {exc}"}

    if not vrp_result["routes"]:
        logger.warning("%s Sin solución VRP: solver_status=%s", prefix, vrp_result["status"])
        return {
            "circuit_id":    circuit_id,
            "status":        "no_solution",
            "solver_status": vrp_result["status"],
            "containers":    len(containers),
        }

    # ── g. Persistir rutas ────────────────────────────────
    _supersede_routes(circuit_id)

    saved: list[dict] = []
    for v_offset, (_, route_indices) in enumerate(sorted(vrp_result["routes"].items())):
        truck_id = truck_ids[min(v_offset, len(truck_ids) - 1)]
        try:
            route_id, dur_s, dist_m = _save_route(
                truck_id           = truck_id,
                circuit_id         = circuit_id,
                route_node_indices = route_indices,
                containers         = containers,
                depot_coords       = depot_coords,
                durations_matrix   = cost_matrix,
                distances_matrix   = distances_matrix,
                vrp_result         = vrp_result,
                bearings           = bearings,
                baseline           = baseline,
            )
        except Exception as exc:
            logger.error("%s Error al guardar ruta para %s: %s", prefix, truck_id, exc, exc_info=True)
            continue

        stops = len([i for i in route_indices
                     if i not in (depot_start_idx, depot_end_idx)])
        route_summary = {
            "route_id":   route_id,
            "truck_id":   truck_id,
            "stops":      stops,
            "duration_s": round(dur_s),
            "distance_m": round(dist_m),
        }
        saved.append(route_summary)
        logger.info(
            "%s Ruta guardada: route_id=%s truck=%s paradas=%d dist=%.1f km dur=%.1f min",
            prefix, route_id, truck_id, stops, dist_m / 1000, dur_s / 60,
        )

        # Notificar al conductor vía WebSocket (fire-and-forget, no bloquea)
        try:
            from ws_notifier import notify_drivers
            ws_stats = notify_drivers(circuit_id=circuit_id, route_summary=route_summary)
            if ws_stats["sent"] > 0:
                logger.info(
                    "%s WebSocket: %d conductor(es) notificado(s)", prefix, ws_stats["sent"]
                )
        except Exception as ws_exc:
            # No fallar la optimización si la notificación falla
            logger.warning("%s ws_notifier error: %s", prefix, ws_exc)

    return {
        "circuit_id":    circuit_id,
        "status":        "optimized",
        "solver":        vrp_result["solver"],
        "solver_status": vrp_result["status"],
        "containers":    len(containers),
        "routes":        saved,
        "total_cost_s":  vrp_result["total_cost"],
    }


# ─────────────────────────────────────────────────────────
# Optimización consolidada de un grupo de circuitos
# ─────────────────────────────────────────────────────────

def _optimize_circuit_group(
    group: CircuitGroup,
    circuit_data: dict[str, dict],
) -> list[dict]:
    """
    Resuelve un único VRP consolidado para todos los circuitos del grupo y
    persiste una ruta por camión. Devuelve un resumen por origin circuit
    (compatible con el formato que produce `_optimize_circuit`).

    Diferencias clave vs. `_optimize_circuit`:
      - Concatena contenedores de todos los origin circuits del grupo.
      - Pool compartido de camiones (todos los idle trucks del grupo).
      - Una sola llamada a OSRM, un solo solve.
      - Baseline calculado por origin circuit usando la matriz consolidada
        (para que `distance_improvement_pct` siga siendo comparable con el
        baseline secuencial CSV del paper).
      - Cada ruta se atribuye al `dominant_circuit_id` (mayoría de stops).
      - Notificación WebSocket fan-out a TODOS los origin circuits que
        aportaron stops a la ruta (no solo al dominante).
    """
    prefix = f"[group={group.group_id}]"

    # ── a. Concatenar contenedores + pool trucks ────────────
    # all_containers_global: todos los activos (matriz OSRM + baseline estático).
    # containers_global:     candidatos (fill > 20%) que recibe el solver.
    all_containers_global: list[dict] = []
    truck_dicts: list[dict] = []
    for cid in group.circuit_ids:
        d = circuit_data[cid]
        all_containers_global.extend(d["containers"])
        truck_dicts.extend(d["trucks"])
    containers_global = [c for c in all_containers_global if _is_candidate(c)]

    if group.total_mandatory < _MIN_CONTAINERS:
        logger.info("%s Saltando — %d obligatorios totales < mínimo %d",
                    prefix, group.total_mandatory, _MIN_CONTAINERS)
        return [{
            "circuit_id": cid,
            "group_id":   group.group_id,
            "status":     "skipped",
            "reason":     f"group_total_mandatory_<_{_MIN_CONTAINERS}",
            "containers": circuit_data[cid]["n_total"],
        } for cid in group.circuit_ids]

    # ── b. Flota agrupada ──────────────────────────────────
    if truck_dicts:
        num_vehicles = len(truck_dicts)
        capacities   = [float(t.get("capacity_kg", _DEFAULT_TRUCK_CAPACITY_KG)) for t in truck_dicts]
        truck_ids    = [t["truck_id"] for t in truck_dicts]
    else:
        # Sin trucks reales: estimar virtual según demanda total
        total_demand = sum(
            estimate_demand_kg(float(c.get("fill_level", 50)), float(c.get("capacity_liters", 2400)))
            for c in containers_global
        )
        num_vehicles = max(1, math.ceil(total_demand / _DEFAULT_TRUCK_CAPACITY_KG))
        capacities   = [_DEFAULT_TRUCK_CAPACITY_KG] * num_vehicles
        truck_ids    = [f"virtual-{group.group_id}-{v}" for v in range(num_vehicles)]

    logger.info("%s %d circuitos, %d contenedores, %d vehículo(s) pooled",
                prefix, len(group.circuit_ids), len(containers_global), num_vehicles)

    # ── c. Depot y locations ───────────────────────────────
    depot_coords = _DEPOTS.get(group.depot_id, _DEPOTS[_DEFAULT_DEPOT_ID])
    all_locations, _, _, full_start_idx, full_end_idx = _build_problem(
        all_containers_global, depot_coords
    )
    _, demands, prizes, depot_start_idx, depot_end_idx = _build_problem(
        containers_global, depot_coords
    )
    nodes = _candidate_nodes(all_containers_global)

    # ── d. OSRM — bearings + matriz ────────────────────────
    try:
        all_bearings = _osrm.get_road_bearings(
            all_locations, depot_indices={full_start_idx, full_end_idx},
        )
    except Exception as exc:
        logger.warning("%s Bearings fallo, continuando sin: %s", prefix, exc)
        all_bearings = None

    try:
        osrm_result = _osrm.get_distance_matrix(all_locations, bearings=all_bearings)
    except (OSRMError, Exception) as exc:
        logger.error("%s OSRM error: %s", prefix, exc, exc_info=True)
        return [{"circuit_id": cid, "group_id": group.group_id, "status": "error",
                 "error": f"osrm: {exc}"} for cid in group.circuit_ids]

    full_cost_matrix = [[int(d) for d in row] for row in osrm_result["durations"]]

    # ── e. Baseline por origin circuit (todos los activos) ──
    baselines_by_circuit = {
        cid: _baseline_for_origin_circuit(
            all_containers_global, cid, full_cost_matrix, osrm_result["distances"],
            full_start_idx, full_end_idx,
        )
        for cid in group.circuit_ids
    }

    cost_matrix      = _submatrix(full_cost_matrix, nodes)
    distances_matrix = _submatrix(osrm_result["distances"], nodes)
    bearings         = [all_bearings[k] for k in nodes] if all_bearings is not None else None
    for cid, bl in baselines_by_circuit.items():
        logger.info(
            "%s Baseline %s: %.1f km, %.1f min, %d paradas",
            prefix, cid,
            bl["baseline_distance_m"] / 1000,
            bl["baseline_duration_s"] / 60,
            bl["baseline_stops"],
        )

    # ── f. VRP solver (una sola llamada) ────────────────────
    try:
        solver = _make_solver()
        vrp_result = solver.solve_vrp(
            cost_matrix     = cost_matrix,
            num_vehicles    = num_vehicles,
            demands         = demands,
            capacities      = capacities,
            depot_start_idx = depot_start_idx,
            depot_end_idx   = depot_end_idx,
            time_limit      = 5,
            prizes          = prizes,
        )
    except Exception as exc:
        logger.error("%s Solver error: %s", prefix, exc, exc_info=True)
        return [{"circuit_id": cid, "group_id": group.group_id, "status": "error",
                 "error": f"solver: {exc}"} for cid in group.circuit_ids]

    if not vrp_result["routes"]:
        logger.warning("%s Sin solución VRP: solver_status=%s",
                       prefix, vrp_result["status"])
        return [{
            "circuit_id":    cid,
            "group_id":      group.group_id,
            "status":        "no_solution",
            "solver_status": vrp_result["status"],
            "containers":    circuit_data[cid]["n_total"],
        } for cid in group.circuit_ids]

    # ── g. Supersede por cada origin circuit ────────────────
    for cid in group.circuit_ids:
        try:
            _supersede_routes(cid)
        except Exception as exc:
            logger.warning("%s supersede %s fallo: %s", prefix, cid, exc)

    # ── h. Persistir rutas (atribución por dominante) ───────
    saved: list[dict] = []
    n_nodes = len(containers_global) + 2  # depot_start + containers + depot_end

    for v_offset, (_, route_indices) in enumerate(sorted(vrp_result["routes"].items())):
        truck_id = truck_ids[min(v_offset, len(truck_ids) - 1)]

        # Stops con origin_circuit_id (excluyendo depósitos)
        depot_set = {depot_start_idx, depot_end_idx}
        origin_ids_in_route: list[str] = []
        for node_idx in route_indices:
            if node_idx in depot_set:
                continue
            c = containers_global[node_idx - 1]
            origin_ids_in_route.append(str(c.get("circuit_id", "")))

        if not origin_ids_in_route:
            continue  # ruta vacía (solo depósitos)

        # Dominante = majority vote
        from collections import Counter
        votes = Counter(origin_ids_in_route)
        dominant_circuit_id, _dom_count = votes.most_common(1)[0]
        distinct_origins = sorted(set(origin_ids_in_route))

        baseline_for_route = baselines_by_circuit.get(dominant_circuit_id)

        try:
            route_id, dur_s, dist_m = _save_route(
                truck_id           = truck_id,
                circuit_id         = dominant_circuit_id,
                route_node_indices = route_indices,
                containers         = containers_global,
                depot_coords       = depot_coords,
                durations_matrix   = cost_matrix,
                distances_matrix   = distances_matrix,
                vrp_result         = vrp_result,
                bearings           = bearings,
                baseline           = baseline_for_route,
                group_id           = group.group_id,
                origin_circuit_ids = distinct_origins,
            )
        except Exception as exc:
            logger.error("%s Error guardando ruta truck=%s: %s",
                         prefix, truck_id, exc, exc_info=True)
            continue

        stops_count = len([i for i in route_indices if i not in depot_set])
        route_summary = {
            "route_id":           route_id,
            "truck_id":           truck_id,
            "stops":              stops_count,
            "duration_s":         round(dur_s),
            "distance_m":         round(dist_m),
            "dominant_circuit":   dominant_circuit_id,
            "origin_circuit_ids": distinct_origins,
        }
        saved.append(route_summary)
        logger.info(
            "%s Ruta guardada: route_id=%s truck=%s paradas=%d dist=%.1f km dur=%.1f min dom=%s origins=%s",
            prefix, route_id, truck_id, stops_count,
            dist_m / 1000, dur_s / 60, dominant_circuit_id, distinct_origins,
        )

        # WebSocket fan-out a TODOS los origin circuits afectados
        try:
            from ws_notifier import notify_drivers
            for cid in distinct_origins:
                ws_stats = notify_drivers(circuit_id=cid, route_summary=route_summary)
                if ws_stats["sent"] > 0:
                    logger.info(
                        "%s WS notif circuit=%s: %d conductor(es)",
                        prefix, cid, ws_stats["sent"],
                    )
        except Exception as ws_exc:
            logger.warning("%s ws_notifier error: %s", prefix, ws_exc)

    # ── i. Resumen por origin circuit ───────────────────────
    # Una entrada por circuit_id; agrupa las rutas que terminaron atribuidas
    # a cada uno (puede ser 0 si todos los containers del circuito fueron
    # absorbidos por rutas dominadas por otros circuitos del grupo).
    results: list[dict] = []
    for cid in group.circuit_ids:
        routes_for_cid = [r for r in saved if r["dominant_circuit"] == cid]
        results.append({
            "circuit_id":    cid,
            "group_id":      group.group_id,
            "status":        "optimized",
            "solver":        vrp_result["solver"],
            "solver_status": vrp_result["status"],
            "containers":    circuit_data[cid]["n_total"],
            "routes":        routes_for_cid,
            "consolidated":  True,
            "group_size":    len(group.circuit_ids),
        })
    return results


# ─────────────────────────────────────────────────────────
# Handler principal
# ─────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Entry point de la Lambda.

    Comportamiento:
      - `{"circuit_id": "..."}`  → optimiza solo ese circuito (path single-circuit,
        no se intenta consolidar).
      - `{"shift": "morning"}` o `{}` → toma todos los circuitos activos del
        turno y los agrupa por depósito antes de optimizar. Grupos con un solo
        circuito caen al path single-circuit; grupos con ≥2 circuitos se
        resuelven con un único solve consolidado (cuOpt/OR-Tools).

    No relanza excepciones para que EventBridge no genere dead-letter events
    por errores de circuitos individuales.
    """
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("[%s] route-optimizer invocado event=%s", request_id, event)

    try:
        circuit_id = event.get("circuit_id")

        # ── Path 1: override single-circuit explícito ─────────
        if circuit_id:
            results = [_optimize_circuit(str(circuit_id))]
        # ── Path 2: shift-based con consolidación ─────────────
        else:
            shift = event.get("shift") or _current_shift()
            logger.info("[%s] Turno: %s", request_id, shift)
            circuit_ids = _get_circuits_for_shift(shift)

            if not circuit_ids:
                logger.info("[%s] Sin circuitos activos para optimizar", request_id)
                return {"statusCode": 200, "message": "no_active_circuits", "results": []}

            # Prefetch containers + trucks → necesario para agrupar por depot
            # y para conocer el tamaño exacto de cada circuito antes del bin-packing.
            circuit_data = _prefetch_circuit_data(circuit_ids)
            groups = _group_circuits_by_depot_shift(circuit_data, shift)

            results = []
            for group in groups:
                if len(group.circuit_ids) < _MIN_CIRCUITS_FOR_CONSOLIDATION:
                    # Fallback single-circuit (1 circuito en el grupo o el threshold lo fuerza).
                    # Usa _optimize_circuit que re-fetcha datos; la duplicación es aceptable
                    # porque ocurre en pocos casos y simplifica el código.
                    for cid in group.circuit_ids:
                        results.append(_optimize_circuit(cid))
                else:
                    results.extend(_optimize_circuit_group(group, circuit_data))

        n_optimized = sum(1 for r in results if r.get("status") == "optimized")
        n_skipped   = sum(1 for r in results if r.get("status") == "skipped")
        n_errors    = sum(1 for r in results if r.get("status") == "error")
        n_consolidated = sum(1 for r in results if r.get("consolidated"))

        logger.info(
            "[%s] Completado: %d circuitos — %d optimizados (%d consolidados), %d saltados, %d errores",
            request_id, len(results), n_optimized, n_consolidated, n_skipped, n_errors,
        )

        return {
            "statusCode":   200,
            "circuits":     len(results),
            "optimized":    n_optimized,
            "consolidated": n_consolidated,
            "skipped":      n_skipped,
            "errors":       n_errors,
            "results":      results,
        }

    except Exception as exc:
        logger.error("[%s] Error inesperado: %s", request_id, exc, exc_info=True)
        return {"statusCode": 500, "error": str(exc)}
