"""
handler.py — SmartWaste MVD / process-sensor-reading

Procesa lecturas de sensores IoT en batch desde SQS y actualiza el estado
operativo de los contenedores en DynamoDB.

Flujo:
  IoT Core MQTT  →  IoT Rule (SQL SELECT *)  →  SQS  →  esta Lambda (batch)
                                                              ↓
                                                    DynamoDB containers  (UpdateItem: estado operativo)
                                                    DynamoDB sensor_readings  (PutItem: time-series)

Cada SQS Record contiene en `body` el payload JSON del mensaje MQTT:
  {
    "container_id": "101941",
    "timestamp":    "2024-01-15T14:30:00+00:00",   # ISO 8601 UTC
    "fill_level":   78.5,                           # 0–100 %
    "battery":      94.2,                           # 0–100 %
    "temperature":  22.0,                           # °C
    "latitude":     -34.835566,
    "longitude":    -56.243533
  }

Campos actualizados en smartwaste-containers:
  fill_level, fill_updated_at, battery, temperature,
  needs_collection, priority

Campos escritos en smartwaste-sensor-readings (time-series con TTL):
  container_id, timestamp, fill_level, priority, ttl
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# ─────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Tablas destino — inyectadas como env vars por Terraform
_CONTAINERS_TABLE: str = os.environ["CONTAINERS_TABLE"]
_SENSOR_READINGS_TABLE: str = os.environ["SENSOR_READINGS_TABLE"]

# Máximo de workers paralelos para las escrituras a DynamoDB.
# 20 workers: cubre un batch de 100 msgs con buen paralelismo sin agotar
# el pool de conexiones (configurado abajo con max_pool_connections=25).
# Nota: DynamoDB BatchWriteItem no soporta UpdateItem, así que usamos
# ThreadPoolExecutor para paralelizar los UpdateItem individuales.
_MAX_WORKERS = 20

# Aumentar el pool de conexiones HTTP para evitar "Connection pool is full"
# cuando _MAX_WORKERS > max_pool_connections (default boto3: 10).
_boto_config = Config(max_pool_connections=25)

# Clientes reutilizados entre invocaciones (Lambda reutiliza el contexto de ejecución)
_dynamodb = boto3.resource("dynamodb", config=_boto_config)
_containers_table = _dynamodb.Table(_CONTAINERS_TABLE)
_sensor_readings_table = _dynamodb.Table(_SENSOR_READINGS_TABLE)

# TTL de lecturas en sensor_readings: 30 días en segundos
_SENSOR_READING_TTL_SECONDS = 30 * 24 * 3600



# ─────────────────────────────────────────────────────────
# Lógica de negocio
# ─────────────────────────────────────────────────────────

def _classify(fill_level: float) -> tuple[str, bool]:
    """
    Determina la prioridad y si el contenedor necesita recolección
    en función del nivel de llenado.

    Returns:
        (priority, needs_collection)

    Reglas:
      fill > 90  → HIGH,   needs_collection = True
      fill > 60  → NORMAL, needs_collection = True
      fill > 30  → NORMAL, needs_collection = False
      fill ≤ 30  → LOW,    needs_collection = False
    """
    if fill_level > 90:
        return "HIGH", True
    if fill_level > 60:
        return "NORMAL", True
    if fill_level > 30:
        return "NORMAL", False
    return "LOW", False


def _to_decimal(value: float | int, precision: int = 1) -> Decimal:
    """Convierte un número a Decimal redondeado. DynamoDB no acepta float."""
    return Decimal(str(round(value, precision)))


# ─────────────────────────────────────────────────────────
# Actualización DynamoDB
# ─────────────────────────────────────────────────────────

def _update_container(
    container_id: str,
    fill_level: float,
    fill_updated_at: str,
    priority: str,
    needs_collection: bool,
    battery: float | None,
    temperature: float | None,
) -> None:
    """
    Actualiza únicamente los campos de estado del contenedor.

    Usa UpdateExpression en lugar de put_item para no sobreescribir
    metadatos estáticos (circuit_id, latitude, depot_name, etc.).

    Raises:
        ClientError: errores de DynamoDB (se propagan al caller para logging).
    """
    set_clauses: list[str] = [
        "fill_level        = :fill_level",
        "fill_updated_at   = :fill_updated_at",
        "priority          = :priority",
        "needs_collection  = :needs_collection",
    ]
    expr_values: dict = {
        ":fill_level":       _to_decimal(fill_level),
        ":fill_updated_at":  fill_updated_at,
        ":priority":         priority,
        ":needs_collection": needs_collection,
    }

    if battery is not None:
        set_clauses.append("battery = :battery")
        expr_values[":battery"] = _to_decimal(battery)

    if temperature is not None:
        set_clauses.append("temperature = :temperature")
        expr_values[":temperature"] = _to_decimal(temperature)

    _containers_table.update_item(
        Key={"container_id": container_id},
        UpdateExpression="SET " + ", ".join(set_clauses),
        ExpressionAttributeValues=expr_values,
        # Falla silenciosamente si el ítem no existe (no se crea uno nuevo vacío)
        ConditionExpression="attribute_exists(container_id)",
        ReturnValues="NONE",
    )


def _put_sensor_reading(
    container_id: str,
    fill_updated_at: str,
    fill_level: float,
    priority: str,
) -> None:
    """
    Escribe una lectura en la tabla time-series sensor_readings.

    La tabla tiene PK=container_id + SK=timestamp. El atributo ttl
    (epoch seconds) permite a DynamoDB eliminar lecturas automáticamente
    pasados 30 días. El historial a largo plazo se conserva en S3 vía
    Kinesis Firehose.

    Raises:
        ClientError: errores de DynamoDB (se propagan al caller).
    """
    ttl = int(datetime.now(tz=timezone.utc).timestamp()) + _SENSOR_READING_TTL_SECONDS

    _sensor_readings_table.put_item(Item={
        "container_id": container_id,
        "timestamp":    fill_updated_at,
        "fill_level":   _to_decimal(fill_level),
        "priority":     priority,
        "ttl":          ttl,
    })


# ─────────────────────────────────────────────────────────
# Procesamiento de un mensaje individual
# ─────────────────────────────────────────────────────────

def _process_single_reading(payload: dict, request_id: str) -> None:
    """
    Procesa un único payload de sensor.

    Extrae los campos, clasifica el nivel de llenado, actualiza la tabla
    `containers` (estado operativo) y escribe en `sensor_readings` (time-series).

    Args:
        payload:    dict con los campos del mensaje MQTT.
        request_id: identificador del request Lambda (para logging).

    Raises:
        ValueError:   campos obligatorios ausentes o inválidos.
        ClientError:  error de DynamoDB.
        Exception:    cualquier error inesperado.
    """
    # ── Extraer campos ────────────────────────────────────
    try:
        container_id = str(payload["container_id"])
        fill_level   = float(payload["fill_level"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Payload inválido — faltan campos obligatorios: {exc}") from exc

    fill_updated_at = (
        str(payload["timestamp"])
        if payload.get("timestamp")
        else datetime.now(tz=timezone.utc).isoformat()
    )

    battery     = payload.get("battery")
    temperature = payload.get("temperature")

    # ── Clasificar nivel ──────────────────────────────────
    priority, needs_collection = _classify(fill_level)

    logger.debug(
        "[%s] container=%s fill=%.1f%% → priority=%s needs_collection=%s",
        request_id, container_id, fill_level, priority, needs_collection,
    )

    # ── Actualizar estado operativo ───────────────────────
    try:
        _update_container(
            container_id     = container_id,
            fill_level       = fill_level,
            fill_updated_at  = fill_updated_at,
            priority         = priority,
            needs_collection = needs_collection,
            battery          = float(battery)     if battery     is not None else None,
            temperature      = float(temperature) if temperature is not None else None,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "[%s] container=%s no existe en %s — ignorando lectura",
                request_id, container_id, _CONTAINERS_TABLE,
            )
            return  # No es un error que deba reintentar SQS
        raise

    # ── Escribir time-series ──────────────────────────────
    _put_sensor_reading(
        container_id    = container_id,
        fill_updated_at = fill_updated_at,
        fill_level      = fill_level,
        priority        = priority,
    )


# ─────────────────────────────────────────────────────────
# Handler principal
# ─────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Entry point de la Lambda — procesa un batch de mensajes SQS.

    El event source mapping entrega hasta 100 mensajes por invocación
    (batch_size=100). Cada mensaje en `event["Records"]` tiene el payload
    MQTT en el campo `body` (string JSON).

    Usa ThreadPoolExecutor para procesar los mensajes en paralelo: las
    escrituras a DynamoDB son la operación más costosa y son independientes
    entre sí, por lo que el paralelismo reduce significativamente la latencia.

    Retorna {"batchItemFailures": [...]} con los messageIds que fallaron.
    SQS reintentará solo esos mensajes, no el batch completo.

    Args:
        event:   {"Records": [{"messageId": ..., "body": "...", ...}, ...]}
        context: contexto Lambda (function_name, aws_request_id, etc.)
    """
    request_id = getattr(context, "aws_request_id", "local")
    records = event.get("Records", [])

    logger.info("[%s] Procesando batch de %d mensajes", request_id, len(records))

    batch_item_failures: list[dict] = []

    def _process_record(record: dict) -> str | None:
        """
        Procesa un Record SQS. Devuelve el messageId si falló, None si tuvo éxito.
        """
        message_id = record["messageId"]
        try:
            payload = json.loads(record["body"])
            _process_single_reading(payload, request_id)
            return None
        except json.JSONDecodeError as exc:
            logger.error(
                "[%s] messageId=%s — body no es JSON válido: %s | body=%s",
                request_id, message_id, exc, record.get("body", "")[:200],
            )
            return message_id
        except ValueError as exc:
            logger.error(
                "[%s] messageId=%s — payload inválido: %s",
                request_id, message_id, exc,
            )
            return message_id
        except ClientError as exc:
            logger.error(
                "[%s] messageId=%s — DynamoDB error: %s",
                request_id, message_id, exc,
                exc_info=True,
            )
            return message_id
        except Exception as exc:
            logger.error(
                "[%s] messageId=%s — error inesperado: %s",
                request_id, message_id, exc,
                exc_info=True,
            )
            return message_id

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_process_record, rec): rec["messageId"] for rec in records}
        for future in as_completed(futures):
            failed_id = future.result()
            if failed_id is not None:
                batch_item_failures.append({"itemIdentifier": failed_id})

    if batch_item_failures:
        logger.warning(
            "[%s] Batch completado: %d/%d mensajes fallaron",
            request_id, len(batch_item_failures), len(records),
        )
    else:
        logger.info(
            "[%s] Batch completado: %d/%d mensajes procesados correctamente",
            request_id, len(records), len(records),
        )

    return {"batchItemFailures": batch_item_failures}
