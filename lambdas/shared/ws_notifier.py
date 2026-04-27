"""
ws_notifier.py — SmartWaste MVD / shared

Notifica a los conductores conectados vía WebSocket cuando se calcula
una nueva ruta para su circuito.

Uso (desde route-optimizer/handler.py):

    from ws_notifier import notify_drivers
    notify_drivers(circuit_id="A_DU_0101", route_summary={...})

Flujo:
  1. Consulta la tabla smartwaste-connections con el GSI circuit-index
     para encontrar todas las conexiones activas del circuito.
  2. Para cada conexión llama a la API Gateway Management API con
     post_to_connection() enviando el payload JSON de la ruta.
  3. Si la conexión está muerta (GoneException / 410), la elimina de
     DynamoDB para no reintentar en llamadas futuras.

Environment vars:
  CONNECTIONS_TABLE   — nombre de la tabla DynamoDB de conexiones
  WS_ENDPOINT         — URL HTTPS del stage WebSocket (para Management API)
                        ej: https://abc123.execute-api.us-east-1.amazonaws.com/dev
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
_WS_ENDPOINT       = os.environ.get("WS_ENDPOINT", "")

_dynamodb          = boto3.resource("dynamodb")


def notify_drivers(circuit_id: str, route_summary: dict[str, Any]) -> dict[str, int]:
    """
    Envía una notificación de nueva ruta a todos los conductores del circuito.

    Args:
        circuit_id:    ID del circuito cuya ruta se actualizó.
        route_summary: Resumen de la ruta recién calculada.
                       Campos esperados: route_id, truck_id, stops,
                       distance_m, duration_s.

    Returns:
        {"sent": N, "failed": M, "stale_removed": K}
        Nunca lanza excepciones — los errores se loguean como WARNING.
    """
    if not _CONNECTIONS_TABLE or not _WS_ENDPOINT:
        logger.warning(
            "ws_notifier: CONNECTIONS_TABLE o WS_ENDPOINT no configurados — skip notificación"
        )
        return {"sent": 0, "failed": 0, "stale_removed": 0}

    connections = _get_connections_for_circuit(circuit_id)

    if not connections:
        logger.info("ws_notifier: sin conexiones activas para circuit_id=%s", circuit_id)
        return {"sent": 0, "failed": 0, "stale_removed": 0}

    message = _build_message(circuit_id, route_summary)

    apigw = boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=_WS_ENDPOINT,
    )

    sent = failed = stale_removed = 0

    for conn in connections:
        connection_id = conn["connection_id"]
        result = _send_to_connection(apigw, connection_id, message)

        if result == "ok":
            sent += 1
        elif result == "stale":
            stale_removed += 1
            _delete_connection(connection_id)
        else:
            failed += 1

    logger.info(
        "ws_notifier: circuit_id=%s — enviado=%d fallido=%d stale_eliminado=%d",
        circuit_id, sent, failed, stale_removed,
    )
    return {"sent": sent, "failed": failed, "stale_removed": stale_removed}


# ─────────────────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────────────────

def _get_connections_for_circuit(circuit_id: str) -> list[dict]:
    """Consulta el GSI circuit-index para obtener conexiones del circuito."""
    tbl = _dynamodb.Table(_CONNECTIONS_TABLE)
    try:
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": "circuit-index",
            "KeyConditionExpression": Key("circuit_id").eq(circuit_id),
            "ProjectionExpression": "connection_id",
        }
        while True:
            resp = tbl.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return items
    except ClientError as exc:
        logger.warning(
            "ws_notifier: error al consultar conexiones circuit_id=%s: %s", circuit_id, exc
        )
        return []


def _build_message(circuit_id: str, route_summary: dict[str, Any]) -> bytes:
    """Construye el payload JSON que se envía al conductor."""
    payload = {
        "type":        "route_update",
        "circuit_id":  circuit_id,
        "route_id":    route_summary.get("route_id", ""),
        "truck_id":    route_summary.get("truck_id", ""),
        "stops":       route_summary.get("stops", 0),
        "distance_km": round(route_summary.get("distance_m", 0) / 1000, 1),
        "duration_min": round(route_summary.get("duration_s", 0) / 60, 1),
        "message":     (
            f"Nueva ruta calculada con {route_summary.get('stops', 0)} paradas. "
            "Actualiza tu app para ver el recorrido."
        ),
    }
    return json.dumps(payload).encode("utf-8")


def _send_to_connection(
    apigw: Any,
    connection_id: str,
    data: bytes,
) -> str:
    """
    Envía datos a una conexión WebSocket.

    Returns:
        "ok"     — enviado correctamente
        "stale"  — conexión ya cerrada (GoneException / 410)
        "error"  — otro error
    """
    try:
        apigw.post_to_connection(ConnectionId=connection_id, Data=data)
        return "ok"
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)

        if code == "GoneException" or status == 410:
            # La conexión ya se cerró sin enviar $disconnect
            logger.debug(
                "ws_notifier: conexión stale eliminada connection_id=%s", connection_id
            )
            return "stale"

        logger.warning(
            "ws_notifier: error enviando a connection_id=%s: %s", connection_id, exc
        )
        return "error"


def _delete_connection(connection_id: str) -> None:
    """Elimina una conexión stale de DynamoDB."""
    tbl = _dynamodb.Table(_CONNECTIONS_TABLE)
    try:
        tbl.delete_item(Key={"connection_id": connection_id})
    except ClientError as exc:
        logger.warning(
            "ws_notifier: no se pudo eliminar conexión stale %s: %s", connection_id, exc
        )
