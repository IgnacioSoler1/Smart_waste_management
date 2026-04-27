"""
handler.py — SmartWaste MVD / websocket-message

Maneja mensajes enviados por el conductor desde la app.

Acciones soportadas:
  container_emptied   → marca el contenedor como vaciado (fill_level=0)

Formato del mensaje (JSON que envía el cliente):
  { "action": "container_emptied", "container_id": "101941" }

El routeSelectionExpression de la API WebSocket es "$request.body.action",
por lo que solo llegan a esta Lambda mensajes con action=container_emptied
(los demás van al $default route, que puede ignorarse o rechazarse).

Environment vars:
  CONTAINERS_TABLE
  CONNECTIONS_TABLE
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_CONTAINERS_TABLE  = os.environ["CONTAINERS_TABLE"]
_CONNECTIONS_TABLE = os.environ["CONNECTIONS_TABLE"]

_dynamodb         = boto3.resource("dynamodb")
_tbl_containers   = _dynamodb.Table(_CONTAINERS_TABLE)
_tbl_connections  = _dynamodb.Table(_CONNECTIONS_TABLE)


def _mark_container_emptied(container_id: str, truck_id: str) -> None:
    """
    Registra que el contenedor fue vaciado por el conductor.

    Actualiza:
      fill_level        = 0
      needs_collection  = False
      priority          = LOW
      last_emptied_at   = ahora (ISO 8601 UTC)
      last_emptied_by   = truck_id
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    _tbl_containers.update_item(
        Key={"container_id": container_id},
        UpdateExpression=(
            "SET fill_level       = :zero,"
            "    needs_collection = :false,"
            "    priority         = :low,"
            "    last_emptied_at  = :now,"
            "    last_emptied_by  = :truck"
        ),
        ExpressionAttributeValues={
            ":zero":  Decimal("0"),
            ":false": False,
            ":low":   "LOW",
            ":now":   now_iso,
            ":truck": truck_id,
        },
        ConditionExpression="attribute_exists(container_id)",
        ReturnValues="NONE",
    )


def lambda_handler(event: dict, context) -> dict:
    ctx           = event["requestContext"]
    connection_id = ctx["connectionId"]
    route_key     = ctx.get("routeKey", "")

    # Parsear body (siempre string en WebSocket proxy events)
    raw_body = event.get("body") or "{}"
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("Mensaje con body inválido connection_id=%s: %r", connection_id, raw_body)
        return {"statusCode": 400}

    action       = body.get("action", route_key)
    container_id = str(body.get("container_id", ""))

    logger.info(
        "WS mensaje: connection_id=%s action=%s container_id=%s",
        connection_id, action, container_id,
    )

    if action != "container_emptied":
        logger.warning("Acción desconocida '%s' de connection_id=%s", action, connection_id)
        return {"statusCode": 400}

    if not container_id:
        logger.warning("Falta container_id en container_emptied (connection_id=%s)", connection_id)
        return {"statusCode": 400}

    # Obtener truck_id desde la tabla de conexiones para registrar quién vació
    truck_id = "unknown"
    try:
        resp = _tbl_connections.get_item(Key={"connection_id": connection_id})
        truck_id = str(resp.get("Item", {}).get("truck_id", "unknown"))
    except ClientError as exc:
        logger.warning("No se pudo obtener truck_id para connection_id=%s: %s", connection_id, exc)

    # Marcar el contenedor como vaciado
    try:
        _mark_container_emptied(container_id, truck_id)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ConditionalCheckFailedException":
            logger.warning(
                "container_id=%s no existe — ignorando container_emptied", container_id
            )
            return {"statusCode": 404}
        logger.error(
            "DynamoDB error al vaciar container_id=%s: %s", container_id, exc, exc_info=True
        )
        return {"statusCode": 500}

    logger.info(
        "container_emptied: container_id=%s truck_id=%s", container_id, truck_id
    )
    return {"statusCode": 200}
