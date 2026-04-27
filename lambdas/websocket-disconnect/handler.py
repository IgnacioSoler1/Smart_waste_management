"""
handler.py — SmartWaste MVD / websocket-disconnect

$disconnect route: elimina la conexión de DynamoDB cuando el cliente cierra.

El ítem también expira por TTL (24 h) pero el $disconnect lo borra activamente
para liberar espacio y evitar que ws_notifier intente enviar a conexiones muertas.

Environment vars:
  CONNECTIONS_TABLE
"""

from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_CONNECTIONS_TABLE = os.environ["CONNECTIONS_TABLE"]

_dynamodb = boto3.resource("dynamodb")
_tbl      = _dynamodb.Table(_CONNECTIONS_TABLE)


def lambda_handler(event: dict, context) -> dict:
    connection_id = event["requestContext"]["connectionId"]

    logger.info("$disconnect: eliminando connection_id=%s", connection_id)

    try:
        _tbl.delete_item(Key={"connection_id": connection_id})
    except ClientError as exc:
        # No bloquear el $disconnect aunque falle DynamoDB — la conexión ya está cerrada
        logger.error(
            "$disconnect DynamoDB error para connection_id=%s: %s",
            connection_id, exc, exc_info=True,
        )

    # API Gateway WebSocket ignora el statusCode en $disconnect,
    # pero devolvemos 200 por consistencia
    return {"statusCode": 200}
