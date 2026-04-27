"""
handler.py — SmartWaste MVD / api

REST API Lambda handler.  Invocado por API Gateway (proxy integration).

Rutas:
  GET  /circuits                   → lista de circuitos con stats agregadas
  GET  /circuits/{id}/containers   → contenedores del circuito con fill_level
  GET  /circuits/{id}/route        → ruta activa más reciente del circuito
  GET  /trucks                     → lista de camiones con posición y estado
  POST /optimize/{circuit_id}      → dispara optimización manual (async)
  GET  /analytics/summary                    → analytics del último día (Glue ETL output)
  GET  /analytics/trends                     → tendencia 30 días por circuito (fill level)
  GET  /analytics/route-efficiency-trends    → tendencia histórica de eficiencia por circuito

Todas las rutas incluyen cabeceras CORS.  El OPTIONS preflight se resuelve
en API Gateway con MOCK integration (no llega a esta Lambda).

Environment vars (inyectadas por Terraform):
  CONTAINERS_TABLE, TRUCKS_TABLE, ROUTES_TABLE
  ROUTE_OPTIMIZER_FUNCTION, DATA_LAKE_BUCKET
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────

_CONTAINERS_TABLE         = os.environ["CONTAINERS_TABLE"]
_TRUCKS_TABLE             = os.environ["TRUCKS_TABLE"]
_ROUTES_TABLE             = os.environ["ROUTES_TABLE"]
_ROUTE_OPTIMIZER_FUNCTION = os.environ["ROUTE_OPTIMIZER_FUNCTION"]
_DATA_LAKE_BUCKET         = os.environ.get("DATA_LAKE_BUCKET", "")

# ─────────────────────────────────────────────────────────
# Clientes AWS (reutilizados entre invocaciones en caliente)
# ─────────────────────────────────────────────────────────

_dynamodb       = boto3.resource("dynamodb")
_tbl_containers = _dynamodb.Table(_CONTAINERS_TABLE)
_tbl_trucks     = _dynamodb.Table(_TRUCKS_TABLE)
_tbl_routes     = _dynamodb.Table(_ROUTES_TABLE)
_lambda_client  = boto3.client("lambda")
_s3_client      = boto3.client("s3")

# ─────────────────────────────────────────────────────────
# Helpers de respuesta y serialización
# ─────────────────────────────────────────────────────────

_CORS_HEADERS: dict[str, str] = {
    "Content-Type":                    "application/json",
    "Access-Control-Allow-Origin":     "*",
    "Access-Control-Allow-Headers":    "Content-Type,Authorization,X-Api-Key,X-Amz-Date",
    "Access-Control-Allow-Methods":    "GET,POST,OPTIONS",
}


class _DecimalEncoder(json.JSONEncoder):
    """JSON encoder que convierte Decimal → float (DynamoDB usa Decimal)."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def _response(status_code: int, body: dict | list) -> dict:
    return {
        "statusCode": status_code,
        "headers":    _CORS_HEADERS,
        "body":       json.dumps(body, cls=_DecimalEncoder),
    }


def _cors_preflight() -> dict:
    """Respuesta para OPTIONS preflight (por si llega alguno a la Lambda)."""
    return {"statusCode": 200, "headers": _CORS_HEADERS, "body": ""}


# ─────────────────────────────────────────────────────────
# Helpers de DynamoDB (paginación automática)
# ─────────────────────────────────────────────────────────

def _scan_all(table: Any, **kwargs: Any) -> list[dict]:
    """Pagina automáticamente sobre Scan hasta agotar resultados."""
    items: list[dict] = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def _query_all(table: Any, **kwargs: Any) -> list[dict]:
    """Pagina automáticamente sobre Query hasta agotar resultados."""
    items: list[dict] = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


# ─────────────────────────────────────────────────────────
# GET /circuits
# ─────────────────────────────────────────────────────────

def _get_circuits() -> dict:
    """
    Escanea la tabla de contenedores y agrega estadísticas por circuito.

    Retorna:
      {
        "count": N,
        "circuits": [
          {
            "circuit_id":       "A_DU_0101",
            "shift":            "morning",
            "total_containers": 98,
            "needs_collection": 34,
            "avg_fill_level":   72.5
          },
          ...
        ]
      }
    """
    items = _scan_all(
        _tbl_containers,
        ProjectionExpression="circuit_id, fill_level, needs_collection, #s, shift",
        ExpressionAttributeNames={"#s": "status"},
    )

    circuits: dict[str, dict] = {}
    for item in items:
        cid = str(item.get("circuit_id", ""))
        if not cid:
            continue

        if cid not in circuits:
            circuits[cid] = {
                "circuit_id":       cid,
                "shift":            str(item.get("shift", "")),
                "total_containers": 0,
                "needs_collection": 0,
                "_fills":           [],
            }

        entry = circuits[cid]
        entry["total_containers"] += 1

        fill = item.get("fill_level")
        if fill is not None:
            entry["_fills"].append(float(fill))

        if item.get("needs_collection") is True:
            entry["needs_collection"] += 1

    result: list[dict] = []
    for entry in sorted(circuits.values(), key=lambda x: x["circuit_id"]):
        fills = entry.pop("_fills")
        entry["avg_fill_level"] = round(sum(fills) / len(fills), 1) if fills else 0.0
        # Conteos por bucket de llenado individual (para pie chart del frontend)
        entry["fill_below_30"] = sum(1 for f in fills if f < 30)
        entry["fill_30_60"]    = sum(1 for f in fills if 30 <= f < 60)
        entry["fill_60_80"]    = sum(1 for f in fills if 60 <= f < 80)
        entry["fill_above_80"] = sum(1 for f in fills if f >= 80)
        result.append(entry)

    logger.info("GET /circuits: %d circuitos", len(result))
    return _response(200, {"count": len(result), "circuits": result})


# ─────────────────────────────────────────────────────────
# GET /circuits/{id}/containers
# ─────────────────────────────────────────────────────────

def _get_circuit_containers(circuit_id: str) -> dict:
    """
    Retorna todos los contenedores del circuito consultando el GSI circuit-index.

    Retorna:
      {
        "circuit_id": "A_DU_0101",
        "count": 98,
        "containers": [ { container_id, fill_level, latitude, longitude, ... }, ... ]
      }
    """
    items = _query_all(
        _tbl_containers,
        IndexName="circuit-index",
        KeyConditionExpression=Key("circuit_id").eq(circuit_id),
    )

    containers = sorted(items, key=lambda c: str(c.get("container_id", "")))

    logger.info("GET /circuits/%s/containers: %d items", circuit_id, len(containers))
    return _response(200, {
        "circuit_id": circuit_id,
        "count":      len(containers),
        "containers": containers,
    })


# ─────────────────────────────────────────────────────────
# GET /circuits/{id}/route
# ─────────────────────────────────────────────────────────

def _get_circuit_route(circuit_id: str) -> dict:
    """
    Retorna todas las rutas activas del circuito.

    Cuando cuOpt divide la recolección en varios camiones, se generan
    múltiples rutas activas para el mismo circuito.  Devolver solo una
    ocultaría las paradas de los demás camiones.

    Usa el GSI circuit-index para Query eficiente en lugar de Scan.

    Retorna 404 si no hay ruta activa para el circuito.
    """
    items = _query_all(
        _tbl_routes,
        IndexName="circuit-index",
        KeyConditionExpression=Key("circuit_id").eq(circuit_id),
        FilterExpression=Attr("status").eq("active"),
    )

    if not items:
        logger.info("GET /circuits/%s/route: sin ruta activa", circuit_id)
        return _response(404, {
            "error":      "No active route found for this circuit",
            "circuit_id": circuit_id,
        })

    # Ordenar por created_at descendente (más recientes primero)
    routes = sorted(items, key=lambda r: str(r.get("created_at", "")), reverse=True)

    logger.info("GET /circuits/%s/route: %d ruta(s) activa(s)", circuit_id, len(routes))
    return _response(200, {
        "circuit_id": circuit_id,
        "count":      len(routes),
        "routes":     routes,
    })


# ─────────────────────────────────────────────────────────
# GET /trucks
# ─────────────────────────────────────────────────────────

def _get_trucks() -> dict:
    """
    Retorna todos los camiones con su estado y posición GPS.

    Retorna:
      {
        "count": N,
        "trucks": [ { truck_id, status, circuit_id, latitude, longitude, ... }, ... ]
      }
    """
    items = _scan_all(_tbl_trucks)
    trucks = sorted(items, key=lambda t: str(t.get("truck_id", "")))

    logger.info("GET /trucks: %d camiones", len(trucks))
    return _response(200, {"count": len(trucks), "trucks": trucks})


# ─────────────────────────────────────────────────────────
# GET /routes/comparison
# ─────────────────────────────────────────────────────────

def _get_routes_comparison() -> dict:
    """
    Compara rutas optimizadas vs baseline (orden CSV original).

    Necesita rutas activas de TODOS los circuitos — no hay partition key útil
    para ese patrón. El Scan es aceptable aquí porque con TTL la tabla se mantiene
    pequeña (~40-100 items activos en estado estacionario).
    """
    items = _scan_all(
        _tbl_routes,
        FilterExpression=(
            Attr("status").eq("active")
            & Attr("baseline_distance_m").exists()
        ),
    )

    if not items:
        return _response(200, {
            "circuits_with_routes": 0,
            "totals": {
                "baseline_distance_km": 0,
                "optimized_distance_km": 0,
                "distance_saved_km": 0,
                "avg_distance_improvement_pct": 0,
                "baseline_duration_min": 0,
                "optimized_duration_min": 0,
                "duration_saved_min": 0,
                "avg_duration_improvement_pct": 0,
                "baseline_stops": 0,
                "optimized_stops": 0,
                "stops_skipped": 0,
            },
            "by_circuit": [],
        })

    # Agrupar por circuit_id.
    # Un circuito puede tener varias rutas activas (una por camion virtual).
    # Sumamos las distancias/duraciones de todos los camiones para obtener
    # el total real del circuito; el baseline es el mismo en todos los registros.
    by_circuit: dict[str, dict] = {}
    for item in items:
        cid = str(item.get("circuit_id", ""))
        if not cid:
            continue
        if cid not in by_circuit:
            by_circuit[cid] = {
                "baseline_distance_m": float(item.get("baseline_distance_m", 0)),
                "baseline_duration_s": float(item.get("baseline_duration_s", 0)),
                "baseline_stops": int(item.get("baseline_stops", 0)),
                "opt_distance_m": 0.0,
                "opt_duration_s": 0.0,
                "opt_stops": 0,
            }
        by_circuit[cid]["opt_distance_m"] += float(item.get("total_distance_m", 0))
        by_circuit[cid]["opt_duration_s"] += float(item.get("total_duration_s", 0))
        by_circuit[cid]["opt_stops"] += len(item.get("stops", []))

    # Calcular metricas por circuito
    circuits_detail: list[dict] = []
    total_baseline_dist = 0.0
    total_opt_dist = 0.0
    total_baseline_dur = 0.0
    total_opt_dur = 0.0
    total_baseline_stops = 0
    total_opt_stops = 0
    dist_improvements: list[float] = []
    dur_improvements: list[float] = []

    for cid, data in sorted(by_circuit.items()):
        bl_dist = data["baseline_distance_m"]
        opt_dist = data["opt_distance_m"]
        bl_dur = data["baseline_duration_s"]
        opt_dur = data["opt_duration_s"]
        bl_stops = data["baseline_stops"]
        opt_stops = data["opt_stops"]
        dist_imp = round((1 - opt_dist / bl_dist) * 100, 1) if bl_dist > 0 else 0.0
        dur_imp = round((1 - opt_dur / bl_dur) * 100, 1) if bl_dur > 0 else 0.0

        total_baseline_dist += bl_dist
        total_opt_dist += opt_dist
        total_baseline_dur += bl_dur
        total_opt_dur += opt_dur
        total_baseline_stops += bl_stops
        total_opt_stops += opt_stops
        dist_improvements.append(dist_imp)
        dur_improvements.append(dur_imp)

        circuits_detail.append({
            "circuit_id": cid,
            "baseline_distance_km": round(bl_dist / 1000, 1),
            "optimized_distance_km": round(opt_dist / 1000, 1),
            "distance_improvement_pct": dist_imp,
            "baseline_duration_min": round(bl_dur / 60, 1),
            "optimized_duration_min": round(opt_dur / 60, 1),
            "duration_improvement_pct": dur_imp,
            "baseline_stops": bl_stops,
            "optimized_stops": opt_stops,
        })

    # Ordenar por mejora de distancia descendente (top circuits first)
    circuits_detail.sort(key=lambda x: x["distance_improvement_pct"], reverse=True)

    n = len(dist_improvements)
    result = {
        "circuits_with_routes": n,
        "totals": {
            "baseline_distance_km": round(total_baseline_dist / 1000, 1),
            "optimized_distance_km": round(total_opt_dist / 1000, 1),
            "distance_saved_km": round((total_baseline_dist - total_opt_dist) / 1000, 1),
            "avg_distance_improvement_pct": round(sum(dist_improvements) / n, 1) if n else 0,
            "baseline_duration_min": round(total_baseline_dur / 60, 1),
            "optimized_duration_min": round(total_opt_dur / 60, 1),
            "duration_saved_min": round((total_baseline_dur - total_opt_dur) / 60, 1),
            "avg_duration_improvement_pct": round(sum(dur_improvements) / n, 1) if n else 0,
            "baseline_stops": total_baseline_stops,
            "optimized_stops": total_opt_stops,
            "stops_skipped": total_baseline_stops - total_opt_stops,
        },
        "by_circuit": circuits_detail,
    }

    logger.info("GET /routes/comparison: %d circuitos con baseline", n)
    return _response(200, result)


# ─────────────────────────────────────────────────────────
# POST /optimize/{circuit_id}
# ─────────────────────────────────────────────────────────

def _trigger_optimize(circuit_id: str) -> dict:
    """
    Invoca la Lambda route-optimizer de forma asíncrona (InvocationType=Event).

    El caller recibe 202 inmediatamente.  La optimización corre en background
    y persiste el resultado en DynamoDB.  Para ver el resultado, el cliente
    puede hacer GET /circuits/{id}/route después de unos segundos.
    """
    payload = json.dumps({"circuit_id": circuit_id})

    resp = _lambda_client.invoke(
        FunctionName=_ROUTE_OPTIMIZER_FUNCTION,
        InvocationType="Event",   # async: no espera resultado, retorna 202
        Payload=payload,
    )

    status = resp["StatusCode"]
    if status != 202:
        logger.error(
            "route-optimizer invoke devolvió %d para circuit_id=%s", status, circuit_id
        )
        return _response(500, {
            "error":      f"Lambda invoke returned unexpected status {status}",
            "circuit_id": circuit_id,
        })

    logger.info("POST /optimize/%s: optimización disparada", circuit_id)
    return _response(202, {
        "message":    "Optimization triggered successfully",
        "circuit_id": circuit_id,
        "info":       "Poll GET /circuits/{id}/route in a few seconds for the result",
    })


# ─────────────────────────────────────────────────────────
# GET /analytics/summary
# ─────────────────────────────────────────────────────────

def _get_analytics_summary() -> dict:
    """Read analytics-results/latest.json from S3 and return it."""
    if not _DATA_LAKE_BUCKET:
        return _response(503, {"error": "Analytics not configured (DATA_LAKE_BUCKET missing)"})

    try:
        obj = _s3_client.get_object(
            Bucket=_DATA_LAKE_BUCKET,
            Key="analytics-results/latest.json",
        )
        body = json.loads(obj["Body"].read())
        logger.info("GET /analytics/summary: date=%s", body.get("date", "?"))
        return _response(200, body)
    except _s3_client.exceptions.NoSuchKey:
        return _response(404, {"error": "No analytics data available yet. Run the Glue ETL job first."})


# ─────────────────────────────────────────────────────────
# GET /analytics/trends
# ─────────────────────────────────────────────────────────

def _get_analytics_trends(circuit_id: str | None, days: int) -> dict:
    """Read latest-trends.json, filter by circuit_id if provided, limit to N days."""
    if not _DATA_LAKE_BUCKET:
        return _response(503, {"error": "Analytics not configured (DATA_LAKE_BUCKET missing)"})

    try:
        obj = _s3_client.get_object(
            Bucket=_DATA_LAKE_BUCKET,
            Key="analytics-results/trends/latest-trends.json",
        )
        all_trends: list[dict] = json.loads(obj["Body"].read())
    except _s3_client.exceptions.NoSuchKey:
        return _response(404, {"error": "No trends data available yet."})

    if circuit_id:
        all_trends = [t for t in all_trends if t.get("circuit_id") == circuit_id]

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    all_trends = [t for t in all_trends if t.get("date", "") >= cutoff]

    logger.info("GET /analytics/trends: circuit_id=%s, days=%d, results=%d", circuit_id, days, len(all_trends))
    return _response(200, {"trends": all_trends})


# ─────────────────────────────────────────────────────────
# GET /analytics/route-efficiency-trends
# ─────────────────────────────────────────────────────────

def _get_route_efficiency_trends(circuit_id: str | None, days: int) -> dict:
    """Read route-efficiency-trends.json, optionally filter by circuit_id and days."""
    if not _DATA_LAKE_BUCKET:
        return _response(503, {"error": "Analytics not configured (DATA_LAKE_BUCKET missing)"})

    try:
        obj = _s3_client.get_object(
            Bucket=_DATA_LAKE_BUCKET,
            Key="analytics-results/route-efficiency-trends.json",
        )
        all_trends: list[dict] = json.loads(obj["Body"].read())
    except _s3_client.exceptions.NoSuchKey:
        return _response(404, {"error": "No route efficiency trends available yet. Run the Glue ETL job first."})

    if circuit_id:
        all_trends = [t for t in all_trends if t.get("circuit_id") == circuit_id]

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    all_trends = [t for t in all_trends if t.get("date", "") >= cutoff]

    logger.info(
        "GET /analytics/route-efficiency-trends: circuit_id=%s, days=%d, results=%d",
        circuit_id, days, len(all_trends),
    )
    return _response(200, {"trends": all_trends})


# ─────────────────────────────────────────────────────────
# Router principal
# ─────────────────────────────────────────────────────────

def lambda_handler(event: dict, context: Any) -> dict:
    """
    Entry point de la Lambda.  Ruteado por httpMethod + path.

    API Gateway (proxy integration) inyecta:
      event["httpMethod"]      → "GET", "POST", "OPTIONS"
      event["path"]            → "/circuits/A_DU_0101/containers"
      event["pathParameters"]  → {"id": "A_DU_0101"} o {"circuit_id": "X"}
    """
    method = event.get("httpMethod", "GET")
    path   = (event.get("path") or "/").rstrip("/") or "/"
    params = event.get("pathParameters") or {}
    qs     = event.get("queryStringParameters") or {}

    logger.info("API %s %s  params=%s qs=%s", method, path, params, qs)

    try:
        # OPTIONS preflight (fallback — normalmente lo resuelve API GW con MOCK)
        if method == "OPTIONS":
            return _cors_preflight()

        # GET /circuits
        if method == "GET" and path == "/circuits":
            return _get_circuits()

        # GET /circuits/{id}/containers
        if method == "GET" and params.get("id") and path.endswith("/containers"):
            return _get_circuit_containers(params["id"])

        # GET /circuits/{id}/route
        if method == "GET" and params.get("id") and path.endswith("/route"):
            return _get_circuit_route(params["id"])

        # GET /routes/comparison
        if method == "GET" and path == "/routes/comparison":
            return _get_routes_comparison()

        # GET /trucks
        if method == "GET" and path == "/trucks":
            return _get_trucks()

        # GET /analytics/summary
        if method == "GET" and path == "/analytics/summary":
            return _get_analytics_summary()

        # GET /analytics/trends
        if method == "GET" and path == "/analytics/trends":
            circuit_id = qs.get("circuit_id")
            days = min(int(qs.get("days", "30")), 365)
            return _get_analytics_trends(circuit_id, days)

        # GET /analytics/route-efficiency-trends
        if method == "GET" and path == "/analytics/route-efficiency-trends":
            circuit_id = qs.get("circuit_id")
            days = min(int(qs.get("days", "30")), 365)
            return _get_route_efficiency_trends(circuit_id, days)

        # POST /optimize/{circuit_id}
        if method == "POST" and params.get("circuit_id"):
            return _trigger_optimize(params["circuit_id"])

        return _response(404, {"error": "Not found", "path": path, "method": method})

    except Exception as exc:
        logger.exception("Unhandled error: %s %s — %s", method, path, exc)
        return _response(500, {"error": "Internal server error", "detail": str(exc)})
