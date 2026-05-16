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

# Depósitos de Montevideo (lat, lon)
_DEPOTS: dict[str, tuple[float, float]] = {
    "felipe_cardoso": (-34.8347, -56.0967),  # noreste — zona este
    "ruta_102":       (-34.8128, -56.2645),  # oeste   — zona oeste
}
_DEFAULT_DEPOT_ID = "felipe_cardoso"

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

def _get_containers(circuit_id: str) -> list[dict]:
    """
    Retorna contenedores activos del circuito con fill_level > 20%.

    Incluye contenedores que no tienen needs_collection=True (fill 20-60%)
    porque el solver usa prizes para decidir si visitarlos en ruta.
    Contenedores con fill ≤ 20% se excluyen (prize=0, no vale la pena).
    """
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "IndexName": "circuit-index",
        "KeyConditionExpression": Key("circuit_id").eq(circuit_id),
        "FilterExpression": (
            Attr("status").eq("active")
            & Attr("fill_level").gt(20)
        ),
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
        containers = _get_containers(circuit_id)
    except Exception as exc:
        logger.error("%s Error al leer contenedores: %s", prefix, exc, exc_info=True)
        return {"circuit_id": circuit_id, "status": "error", "error": f"dynamo: {exc}"}

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
    depot_id     = str(containers[0].get("depot_id", _DEFAULT_DEPOT_ID))
    depot_coords = _DEPOTS.get(depot_id, _DEPOTS[_DEFAULT_DEPOT_ID])
    locations, demands, prizes, depot_start_idx, depot_end_idx = _build_problem(
        containers, depot_coords
    )

    # ── e. OSRM — bearings + matriz de distancias ─────────
    depot_indices = {depot_start_idx, depot_end_idx}
    try:
        bearings = _osrm.get_road_bearings(
            locations, depot_indices=depot_indices,
        )
    except Exception as exc:
        logger.warning("%s No se pudieron obtener bearings, continuando sin ellos: %s", prefix, exc)
        bearings = None

    try:
        osrm_result = _osrm.get_distance_matrix(locations, bearings=bearings)
    except OSRMError as exc:
        logger.error("%s OSRM error: %s", prefix, exc)
        return {"circuit_id": circuit_id, "status": "error", "error": f"osrm: {exc}"}
    except Exception as exc:
        logger.error("%s OSRM falla inesperada: %s", prefix, exc, exc_info=True)
        return {"circuit_id": circuit_id, "status": "error", "error": f"osrm: {exc}"}

    cost_matrix = [[int(d) for d in row] for row in osrm_result["durations"]]

    # ── e2. Baseline: ruta original (orden CSV) ────────────
    baseline = _compute_baseline_csv_order(
        containers, cost_matrix, osrm_result["distances"],
        depot_start_idx, depot_end_idx,
    )
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
                distances_matrix   = osrm_result["distances"],
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
# Handler principal
# ─────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Entry point de la Lambda.

    No relanza excepciones para que EventBridge no genere dead-letter events
    por errores de circuitos individuales.
    """
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("[%s] route-optimizer invocado event=%s", request_id, event)

    try:
        circuit_id = event.get("circuit_id")

        if circuit_id:
            circuits = [str(circuit_id)]
        else:
            shift    = event.get("shift") or _current_shift()
            logger.info("[%s] Turno: %s", request_id, shift)
            circuits = _get_circuits_for_shift(shift)

        if not circuits:
            logger.info("[%s] Sin circuitos activos para optimizar", request_id)
            return {"statusCode": 200, "message": "no_active_circuits", "results": []}

        results = [_optimize_circuit(cid) for cid in circuits]

        n_optimized = sum(1 for r in results if r.get("status") == "optimized")
        n_skipped   = sum(1 for r in results if r.get("status") == "skipped")
        n_errors    = sum(1 for r in results if r.get("status") == "error")

        logger.info(
            "[%s] Completado: %d circuitos — %d optimizados, %d saltados, %d errores",
            request_id, len(results), n_optimized, n_skipped, n_errors,
        )

        return {
            "statusCode": 200,
            "circuits":   len(results),
            "optimized":  n_optimized,
            "skipped":    n_skipped,
            "errors":     n_errors,
            "results":    results,
        }

    except Exception as exc:
        logger.error("[%s] Error inesperado: %s", request_id, exc, exc_info=True)
        return {"statusCode": 500, "error": str(exc)}
