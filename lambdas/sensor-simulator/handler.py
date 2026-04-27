"""
handler.py — SmartWaste MVD / sensor-simulator

Lambda que simula sensores IoT publicando lecturas de fill_level a IoT Core.
Se ejecuta cada 10 min vía EventBridge.

Flujo:
  EventBridge (rate 10 min)  →  esta Lambda
    1. Lee contenedores activos de DynamoDB
    2. Calcula fill_level usando FillModel (curva exponencial-saturante)
    3. Publica cada lectura a IoT Core vía boto3 iot-data
    4. IoT Rule reenvía a process-sensor-reading Lambda → DynamoDB
    5. IoT Rule reenvía a Kinesis → Firehose → S3

Estado:
  - `last_emptied` en DynamoDB: referencia para calcular horas transcurridas.
    Si es NULL, se asume 24 h (estado inicial desconocido).
  - `fill_updated_at`: se usa como fallback si last_emptied es NULL.

Environment vars:
  CONTAINERS_TABLE  — tabla DynamoDB de contenedores
  IOT_TOPIC_PREFIX  — prefijo del topic MQTT (default: smartwaste-dev/sensors)
  IOT_ENDPOINT      — endpoint IoT Core (opcional, boto3 lo resuelve por región)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

# fill_model y zone_density se copian al build dir por build.sh
from fill_model import FillModel
from zone_density import get_zone_factor

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────

_CONTAINERS_TABLE = os.environ["CONTAINERS_TABLE"]
_IOT_TOPIC_PREFIX = os.environ.get("IOT_TOPIC_PREFIX", "smartwaste-dev/sensors")

# Temperatura base por mes en Montevideo (promedio max/min histórica)
_MONTHLY_TEMP: dict[int, float] = {
    1: 23.5,  2: 23.0,  3: 21.0,  4: 17.5,
    5: 13.0,  6: 10.5,  7: 10.5,  8: 11.0,
    9: 13.0, 10: 16.5, 11: 19.5, 12: 22.0,
}

# ─────────────────────────────────────────────────────────
# Clientes AWS (reutilizados entre invocaciones)
# ─────────────────────────────────────────────────────────

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_CONTAINERS_TABLE)
_iot_data = boto3.client("iot-data")
# base_rate=2.2 (vs 2.0 default) → promedio de fill ~60%
# noise_std=4.0 (vs 2.0 default) → más variación entre contenedores vecinos
_fill_model = FillModel(base_rate=2.2, noise_std=4.0)


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _simulate_temperature(now: datetime) -> float:
    base = _MONTHLY_TEMP[now.month]
    return round(base + random.gauss(0.0, 2.0), 1)


def _simulate_battery(container_id: str) -> float:
    """Batería simulada estable por contenedor (hash-based)."""
    h = hash(container_id) % 10000
    return round(85.0 + (h / 10000.0) * 15.0, 1)


def _container_hash_frac(container_id: str, salt: str = "") -> float:
    """Retorna un float determinista en [0, 1) basado en el container_id.

    Usa MD5 con un salt para generar múltiples valores independientes
    por container. Determinista: el mismo container siempre da el mismo
    valor, pero containers distintos varían uniformemente.
    """
    digest = hashlib.md5(f"{container_id}:{salt}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _fallback_hours_since_empty(container_id: str) -> float:
    """Horas desde el último vaciado cuando no hay last_emptied en DynamoDB.

    Distribuye los contenedores entre 4h y 96h de forma determinista
    pero con distribución sesgada hacia valores altos (más contenedores
    llenos que vacíos, reflejando que la recolección no es continua).

    Distribución uniforme [4, 60] → mediana ~32h, promedio ~32h
    """
    frac = _container_hash_frac(container_id, salt="hours")
    return 4.0 + frac * 56.0


def _container_rate_jitter(container_id: str) -> float:
    """Factor multiplicativo per-container para variar la tasa de llenado.

    Rango: 0.6 a 1.5 — simula diferencias locales dentro del mismo barrio:
    - Contenedor frente a un supermercado → 1.5x
    - Contenedor en calle residencial tranquila → 0.6x
    - La mayoría quedan entre 0.8 y 1.3

    Determinista por container_id para consistencia entre invocaciones.
    """
    frac = _container_hash_frac(container_id, salt="jitter")
    return 0.6 + frac * 0.9


def _parse_last_emptied(item: dict) -> datetime | None:
    """Extrae last_emptied del item DynamoDB."""
    val = item.get("last_emptied")
    if val is None:
        return None
    try:
        return datetime.fromisoformat(str(val))
    except (ValueError, TypeError):
        return None


def _get_all_active_containers() -> list[dict]:
    """Scan paginado de todos los contenedores activos."""
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("status").eq("active"),
    }
    while True:
        resp = _table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


# ─────────────────────────────────────────────────────────
# Handler principal
# ─────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    request_id = getattr(context, "aws_request_id", "local")
    now = datetime.now(tz=timezone.utc)
    logger.info("[%s] sensor-simulator invocado", request_id)

    # ── Leer contenedores ─────────────────────────────────
    try:
        containers = _get_all_active_containers()
    except Exception as exc:
        logger.error("[%s] Error leyendo contenedores: %s", request_id, exc)
        return {"statusCode": 500, "error": str(exc)}

    logger.info("[%s] %d contenedores activos", request_id, len(containers))

    if not containers:
        return {"statusCode": 200, "published": 0, "message": "no_active_containers"}

    # ── Simular y publicar ────────────────────────────────
    published = 0
    errors = 0

    for c in containers:
        container_id = str(c["container_id"])

        try:
            # Determinar last_emptied
            last_emptied = _parse_last_emptied(c)
            if last_emptied is None:
                # Fallback: horas variadas per-container (4h–96h)
                # en vez de 24h fijo para todos, generando diversidad
                fut = c.get("fill_updated_at")
                if fut:
                    try:
                        hours_back = _fallback_hours_since_empty(container_id)
                        last_emptied = datetime.fromisoformat(str(fut)) - timedelta(hours=hours_back)
                    except (ValueError, TypeError):
                        last_emptied = now - timedelta(hours=_fallback_hours_since_empty(container_id))
                else:
                    last_emptied = now - timedelta(hours=_fallback_hours_since_empty(container_id))

            # Calcular fill level con jitter per-container
            # El jitter simula variación local: un contenedor frente a
            # un comercio se llena más rápido que uno en calle tranquila,
            # aunque estén en el mismo barrio.
            jitter = _container_rate_jitter(container_id)
            container_info = {
                "latitude": float(c.get("latitude", -34.9)),
                "longitude": float(c.get("longitude", -56.1)),
                "zone_factor": get_zone_factor(
                    float(c.get("latitude", -34.9)),
                    float(c.get("longitude", -56.1)),
                ) * jitter,
            }
            fill_level = _fill_model.calculate_fill_level(
                container_info=container_info,
                current_time=now,
                last_emptied_time=last_emptied,
            )

            # Construir payload
            payload = {
                "container_id": container_id,
                "timestamp": now.isoformat(),
                "fill_level": round(fill_level, 1),
                "battery": _simulate_battery(container_id),
                "temperature": _simulate_temperature(now),
                "latitude": float(c.get("latitude", -34.9)),
                "longitude": float(c.get("longitude", -56.1)),
            }

            # Publicar a IoT Core
            topic = f"{_IOT_TOPIC_PREFIX}/{container_id}"
            _iot_data.publish(
                topic=topic,
                qos=0,
                payload=json.dumps(payload),
            )
            published += 1

        except Exception as exc:
            errors += 1
            if errors <= 5:
                logger.warning(
                    "[%s] Error publicando %s: %s", request_id, container_id, exc
                )

    logger.info(
        "[%s] Completado: %d publicados, %d errores de %d contenedores",
        request_id, published, errors, len(containers),
    )

    return {
        "statusCode": 200,
        "published": published,
        "errors": errors,
        "total_containers": len(containers),
    }
