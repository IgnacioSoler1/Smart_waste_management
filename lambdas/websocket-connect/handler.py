"""
handler.py — SmartWaste MVD / websocket-connect

$connect route: persiste la conexión WebSocket en DynamoDB.

El conductor pasa truck_id y circuit_id como query strings al conectarse:
  wss://<api>.execute-api.us-east-1.amazonaws.com/dev?truck_id=T01&circuit_id=A_DU_0101

La tabla smartwaste-connections almacena el mapeo connection_id → camión/circuito.
Otros componentes (ws_notifier) la consultan para saber a qué conexiones enviar
actualizaciones de ruta.

TTL: 24 h desde la conexión. Las conexiones inactivas se borran automáticamente
por DynamoDB; el $disconnect las borra activamente cuando el cliente cierra.

Environment vars:
  CONNECTIONS_TABLE
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_CONNECTIONS_TABLE = os.environ["CONNECTIONS_TABLE"]

_dynamodb = boto3.resource("dynamodb")
_tbl      = _dynamodb.Table(_CONNECTIONS_TABLE)

_TTL_HOURS = 24


def lambda_handler(event: dict, context) -> dict:
    ctx           = event["requestContext"]
    connection_id = ctx["connectionId"]
    stage         = ctx.get("stage", "dev")
    domain        = ctx.get("domainName", "")

    query_params  = event.get("queryStringParameters") or {}
    truck_id      = str(query_params.get("truck_id", ""))
    circuit_id    = str(query_params.get("circuit_id", ""))

    now       = datetime.now(timezone.utc)
    ttl_epoch = int((now + timedelta(hours=_TTL_HOURS)).timestamp())

    logger.info(
        "$connect: connection_id=%s truck_id=%s circuit_id=%s",
        connection_id, truck_id, circuit_id,
    )

    # truck_id es obligatorio — sin él no podemos asociar la conexión a un camión
    if not truck_id:
        logger.warning(
            "$connect rechazado — falta truck_id (connection_id=%s)", connection_id
        )
        return {"statusCode": 400}

    try:
        _tbl.put_item(Item={
            "connection_id": connection_id,
            "truck_id":      truck_id,
            "circuit_id":    circuit_id,
            "connected_at":  now.isoformat(),
            "domain":        domain,
            "stage":         stage,
            "ttl":           Decimal(ttl_epoch),
        })
    except ClientError as exc:
        logger.error(
            "$connect DynamoDB error para connection_id=%s: %s",
            connection_id, exc, exc_info=True,
        )
        # Devolver 500 rechaza la conexión WebSocket
        return {"statusCode": 500}

    logger.info("$connect: conexión registrada connection_id=%s", connection_id)
    return {"statusCode": 200}
