"""
simulator.py — SmartWaste MVD

Simulador de sensores IoT para contenedores de residuos domiciliarios.

En cada ciclo publica lecturas de fill_level, batería y temperatura a
AWS IoT Core (o imprime a stdout en modo --dry-run).

Uso típico:
  # Simular todos los contenedores, leyendo de DynamoDB, publicando a IoT Core
  python -m simulator.simulator \\
      --endpoint xxxx-ats.iot.us-east-1.amazonaws.com \\
      --cert  certs/device.pem.crt \\
      --key   certs/device.pem.key \\
      --ca    certs/AmazonRootCA1.pem

  # Solo un circuito, modo local (sin AWS), solo imprimir:
  python -m simulator.simulator \\
      --circuit A_DU_RM_CL_109 --local --dry-run --interval 10

Dependencias: boto3, awsiotsdk
"""

import argparse
import json
import logging
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from simulator.fill_model import FillModel
from simulator.zone_density import get_zone_factor

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────

DEFAULT_TABLE      = "smartwaste-containers"
DEFAULT_PROFILE    = "personal-smart-recycle"
DEFAULT_REGION     = "us-east-1"
DEFAULT_INTERVAL   = 300          # segundos entre ciclos
DEFAULT_DATA_DIR   = "data/processed"
MQTT_TOPIC_PREFIX  = "smartwaste-dev/sensors"

# Temperatura base por mes en Montevideo (lat -34°S): promedio entre
# máxima y mínima histórica. Índice 1-12.
_MONTHLY_TEMP: dict[int, float] = {
    1: 23.5,  2: 23.0,  3: 21.0,  4: 17.5,
    5: 13.0,  6: 10.5,  7: 10.5,  8: 11.0,
    9: 13.0, 10: 16.5, 11: 19.5, 12: 22.0,
}


# ─────────────────────────────────────────────────────────
# Estado por contenedor
# ─────────────────────────────────────────────────────────

@dataclass
class ContainerState:
    """
    Estado mutable de un contenedor en la simulación.

    Campos que vienen del JSON / DynamoDB:
      container_id, circuit_id, latitude, longitude, shift,
      collection_days, zone, depot_id, capacity_liters

    Campos gestionados por el simulador:
      zone_factor:   calculado al cargar, usado en cada ciclo
      last_emptied:  datetime UTC, inicializado con offset aleatorio
      battery:       % de batería, 85-100% inicial, baja ~0.03% por ciclo
      _battery_drift: variación aleatoria asignada al contenedor
    """

    container_id:    str
    circuit_id:      str
    latitude:        float
    longitude:       float
    shift:           str
    collection_days: list[str]
    zone:            str
    depot_id:        str
    capacity_liters: int
    zone_factor:     float
    last_emptied:    datetime
    battery:         float = field(default_factory=lambda: random.uniform(85.0, 100.0))
    _battery_drift:  float = field(default_factory=lambda: random.uniform(0.02, 0.05))

    def drain_battery(self) -> None:
        """Reduce la batería en un pequeño incremento aleatorio por ciclo."""
        self.battery = max(0.0, self.battery - self._battery_drift)
        # Simular reemplazo de batería cuando llega al 5%
        if self.battery < 5.0:
            self.battery = random.uniform(95.0, 100.0)
            logger.debug("container %s: batería reemplazada", self.container_id)


# ─────────────────────────────────────────────────────────
# Carga de contenedores
# ─────────────────────────────────────────────────────────

def _make_state(raw: dict[str, Any], now: datetime) -> ContainerState:
    """
    Construye un ContainerState desde un dict (JSON o DynamoDB).

    El `last_emptied` se inicializa con un offset aleatorio entre 0 y 48 horas
    en el pasado para que los contenedores empiecen con niveles variados.
    """
    lat = float(raw["latitude"])
    lon = float(raw["longitude"])

    initial_offset_hours = random.uniform(0.0, 48.0)
    last_emptied = now - timedelta(hours=initial_offset_hours)

    return ContainerState(
        container_id    = str(raw["container_id"]),
        circuit_id      = str(raw["circuit_id"]),
        latitude        = lat,
        longitude       = lon,
        shift           = str(raw.get("shift", "UNKNOWN")),
        collection_days = list(raw.get("collection_days") or []),
        zone            = str(raw.get("zone", "")),
        depot_id        = str(raw.get("depot_id", "")),
        capacity_liters = int(raw.get("capacity_liters", 2400)),
        zone_factor     = float(raw.get("zone_factor") or get_zone_factor(lat, lon)),
        last_emptied    = last_emptied,
    )


def load_from_local(
    data_dir: str,
    circuit_filter: str,
) -> list[ContainerState]:
    """
    Lee containers_enriched.json y construye la lista de estados.

    Args:
        data_dir:       directorio con los archivos procesados
        circuit_filter: "all" o un circuit_id específico

    Returns:
        Lista de ContainerState con last_emptied aleatorio.
    """
    path = Path(data_dir) / "containers_enriched.json"
    if not path.exists():
        logger.error("Archivo no encontrado: %s", path)
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        raw_list: list[dict] = json.load(f)

    now = datetime.now(tz=timezone.utc)
    states: list[ContainerState] = []

    for raw in raw_list:
        if circuit_filter != "all" and raw.get("circuit_id", "").strip() != circuit_filter:
            continue
        if str(raw.get("status", "active")) != "active":
            continue
        states.append(_make_state(raw, now))

    return states


def load_from_dynamodb(
    table_name: str,
    circuit_filter: str,
    profile: str | None,
    region: str,
) -> list[ContainerState]:
    """
    Lee contenedores activos desde DynamoDB.

    Si circuit_filter != "all", usa el GSI circuit-index para una query
    eficiente. Si es "all", hace un Scan paginado de la tabla completa.

    Args:
        table_name:     nombre de la tabla DynamoDB
        circuit_filter: "all" o circuit_id específico
        profile:        perfil AWS CLI (None = credenciales del entorno)
        region:         región AWS

    Returns:
        Lista de ContainerState con last_emptied aleatorio.

    Raises:
        SystemExit: si la tabla no existe o hay error de credenciales.
    """
    session_kwargs: dict[str, Any] = {"region_name": region}
    if profile:
        session_kwargs["profile_name"] = profile

    try:
        session = boto3.Session(**session_kwargs)
        dynamodb = session.resource("dynamodb")
        table = dynamodb.Table(table_name)
        # Verificar que la tabla existe
        table.load()
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            logger.error("Tabla '%s' no existe. Ejecutar 'terraform apply' primero.", table_name)
        else:
            logger.error("Error DynamoDB: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("Error al conectar con AWS: %s", exc)
        sys.exit(1)

    now = datetime.now(tz=timezone.utc)
    items: list[dict] = []

    try:
        if circuit_filter != "all":
            # Query eficiente por GSI
            response = table.query(
                IndexName="circuit-index",
                KeyConditionExpression=Key("circuit_id").eq(circuit_filter),
                FilterExpression=Attr("status").eq("active"),
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.query(
                    IndexName="circuit-index",
                    KeyConditionExpression=Key("circuit_id").eq(circuit_filter),
                    FilterExpression=Attr("status").eq("active"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
        else:
            # Scan paginado
            response = table.scan(FilterExpression=Attr("status").eq("active"))
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("status").eq("active"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

    except ClientError as exc:
        logger.error("Error al leer DynamoDB: %s", exc)
        sys.exit(1)

    return [_make_state(item, now) for item in items]


# ─────────────────────────────────────────────────────────
# Sensores físicos simulados
# ─────────────────────────────────────────────────────────

def simulate_temperature(now: datetime) -> float:
    """
    Temperatura ambiente simulada en °C según el mes del año (Montevideo).

    Usa la temperatura promedio histórica del mes + ruido gaussiano std=2°C.
    Rango típico de salida: 6°C (invierno) a 28°C (verano).
    """
    base = _MONTHLY_TEMP[now.month]
    return round(base + random.gauss(0.0, 2.0), 1)


# ─────────────────────────────────────────────────────────
# Construcción del payload MQTT
# ─────────────────────────────────────────────────────────

def build_payload(
    state: ContainerState,
    fill_level: float,
    now: datetime,
) -> dict[str, Any]:
    """
    Construye el payload JSON a publicar en IoT Core.

    Esquema:
      {
        "container_id": "101941",
        "timestamp":    "2024-01-15T14:30:00+00:00",
        "fill_level":   78.5,          # 0-100 %
        "battery":      94.2,          # 0-100 %
        "temperature":  22.0,          # °C
        "latitude":     -34.835566,
        "longitude":    -56.243533
      }
    """
    return {
        "container_id": state.container_id,
        "timestamp":    now.isoformat(),
        "fill_level":   round(fill_level, 1),
        "battery":      round(state.battery, 1),
        "temperature":  simulate_temperature(now),
        "latitude":     state.latitude,
        "longitude":    state.longitude,
    }


# ─────────────────────────────────────────────────────────
# Loop principal
# ─────────────────────────────────────────────────────────

def run(
    states: list[ContainerState],
    fill_model: FillModel,
    publisher,             # MQTTPublisher | None (None en dry-run)
    interval: int,
    dry_run: bool,
) -> None:
    """
    Loop de simulación. Se ejecuta hasta recibir SIGINT (Ctrl+C).

    En cada ciclo:
      1. Calcula fill_level para cada contenedor con FillModel
      2. Construye el payload MQTT
      3. Publica (o imprime en dry-run)
      4. Duerme `interval` segundos

    El estado de `last_emptied` se mantiene en memoria. La batería
    baja ~0.03% por ciclo. La temperatura sigue el calendario.
    """
    # Flag para salida limpia ante Ctrl+C
    _stop = {"flag": False}

    def _handle_sigint(signum, frame):
        print("\n  Señal SIGINT recibida — terminando tras el ciclo actual…")
        _stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigint)

    cycle = 0
    total_published = 0
    total_errors = 0

    print(f"\n  Simulando {len(states):,} contenedores")
    print(f"  Intervalo: {interval}s  |  Modo: {'DRY-RUN' if dry_run else 'MQTT'}")
    print(f"  Ctrl+C para detener\n")

    while not _stop["flag"]:
        cycle += 1
        now = datetime.now(tz=timezone.utc)
        cycle_published = 0
        cycle_errors = 0

        print(f"  ── Ciclo {cycle}  {now.strftime('%Y-%m-%d %H:%M:%S UTC')} ──")

        for state in states:
            if _stop["flag"]:
                break

            # ── Calcular nivel de llenado ────────────────
            fill = fill_model.calculate_fill_level(
                container_info  = {"latitude": state.latitude, "longitude": state.longitude,
                                   "zone_factor": state.zone_factor},
                current_time    = now,
                last_emptied_time = state.last_emptied,
            )

            # ── Simular batería ──────────────────────────
            state.drain_battery()

            # ── Construir payload ────────────────────────
            payload = build_payload(state, fill, now)
            topic   = f"{MQTT_TOPIC_PREFIX}/{state.container_id}"

            # ── Publicar o imprimir ──────────────────────
            if dry_run:
                print(
                    f"    [{state.container_id}] topic={topic}  "
                    f"fill={fill:.1f}%  bat={state.battery:.1f}%  "
                    f"temp={payload['temperature']}°C"
                )
                cycle_published += 1
            else:
                try:
                    publisher.publish(topic, payload)
                    cycle_published += 1
                    logger.debug("published %s → fill=%.1f%%", state.container_id, fill)
                except Exception as exc:
                    cycle_errors += 1
                    total_errors += 1
                    logger.warning(
                        "Error publicando %s: %s", state.container_id, exc
                    )

        total_published += cycle_published

        # ── Resumen del ciclo ────────────────────────────
        print(
            f"    → {cycle_published} publicados"
            + (f"  {cycle_errors} errores" if cycle_errors else "")
            + f"  (total: {total_published})"
        )

        if _stop["flag"]:
            break

        # ── Esperar hasta el próximo ciclo ───────────────
        print(f"    → esperando {interval}s…")
        # Dormir en fragmentos para reaccionar rápido a Ctrl+C
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and not _stop["flag"]:
            time.sleep(min(1.0, deadline - time.monotonic()))

    # ── Fin del loop ─────────────────────────────────────
    print(f"\n  Simulación terminada.")
    print(f"  Ciclos completados: {cycle - (1 if _stop['flag'] else 0)}")
    print(f"  Mensajes publicados: {total_published}")
    if total_errors:
        print(f"  Errores de publicación: {total_errors}")


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silenciar logs verbosos del SDK de AWS
    logging.getLogger("awscrt").setLevel(logging.WARNING)
    logging.getLogger("awsiot").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description="Simulador de sensores IoT para SmartWaste MVD.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  # Dry-run local de un circuito (sin AWS):
  python -m simulator.simulator --circuit A_DU_RM_CL_109 --local --dry-run --interval 5

  # Todos los contenedores desde DynamoDB, publicando a IoT Core:
  python -m simulator.simulator \\
      --endpoint xxxx-ats.iot.us-east-1.amazonaws.com \\
      --cert  certs/device.pem.crt \\
      --key   certs/device.pem.key \\
      --ca    certs/AmazonRootCA1.pem

  # Un circuito desde DynamoDB, dry-run (no necesita certs):
  python -m simulator.simulator --circuit B_DU_RM_CL_001 --dry-run
        """,
    )

    # ── Fuente de datos ──────────────────────────────────
    parser.add_argument(
        "--circuit",
        default="all",
        help="Circuito a simular (default: 'all'). Ejemplo: A_DU_RM_CL_109",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Leer contenedores desde containers_enriched.json en vez de DynamoDB",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        dest="data_dir",
        help=f"Directorio de archivos procesados para --local (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--table-name",
        default=DEFAULT_TABLE,
        dest="table_name",
        help=f"Nombre de la tabla DynamoDB (default: {DEFAULT_TABLE})",
    )

    # ── Simulación ───────────────────────────────────────
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"Segundos entre ciclos de publicación (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Imprimir payloads a stdout en vez de publicar MQTT",
    )

    # ── AWS IoT Core ─────────────────────────────────────
    parser.add_argument(
        "--endpoint",
        default=None,
        help="IoT Core ATS endpoint. Obligatorio si no es --dry-run",
    )
    parser.add_argument(
        "--cert",
        default=None,
        help="Path al certificado del dispositivo (.pem.crt)",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="Path a la clave privada (.pem.key)",
    )
    parser.add_argument(
        "--ca",
        default="certs/AmazonRootCA1.pem",
        help="Path al CA raíz de Amazon (default: certs/AmazonRootCA1.pem)",
    )
    parser.add_argument(
        "--client-id",
        default=None,
        dest="client_id",
        help="ID MQTT del cliente (default: smartwaste-sim-{hostname}-{pid})",
    )

    # ── AWS credentials ───────────────────────────────────
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"Perfil AWS CLI para DynamoDB (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"Región AWS (default: {DEFAULT_REGION})",
    )

    args = parser.parse_args()

    print("=" * 58)
    print("  SmartWaste MVD — Simulador de sensores IoT")
    print("=" * 58)

    # ── Validaciones ─────────────────────────────────────
    if not args.dry_run:
        missing = [
            flag for flag, val in [
                ("--endpoint", args.endpoint),
                ("--cert",     args.cert),
                ("--key",      args.key),
            ]
            if val is None
        ]
        if missing:
            parser.error(
                f"Los siguientes flags son obligatorios en modo MQTT: "
                f"{', '.join(missing)}\n"
                f"Usar --dry-run para modo sin IoT Core."
            )

    # ── Cargar contenedores ───────────────────────────────
    if args.local:
        print(f"\n  Fuente: JSON local ({args.data_dir})")
        states = load_from_local(args.data_dir, args.circuit)
    else:
        print(f"\n  Fuente: DynamoDB ({args.table_name})")
        states = load_from_dynamodb(
            args.table_name, args.circuit, args.profile, args.region
        )

    if not states:
        filter_msg = (
            f"circuito '{args.circuit}'" if args.circuit != "all" else "ningún circuito"
        )
        print(f"\n❌ No se encontraron contenedores activos para {filter_msg}.")
        sys.exit(1)

    print(f"  Contenedores cargados: {len(states):,}")

    # ── FillModel ─────────────────────────────────────────
    fill_model = FillModel()

    # ── MQTTPublisher ─────────────────────────────────────
    publisher = None
    if not args.dry_run:
        from simulator.mqtt_publisher import MQTTPublisher

        publisher = MQTTPublisher(
            endpoint  = args.endpoint,
            cert_path = args.cert,
            key_path  = args.key,
            ca_path   = args.ca,
            client_id = args.client_id,
        )
        print(f"\n  Conectando a IoT Core: {args.endpoint}…")
        try:
            publisher.connect()
            print("  Conectado ✓")
        except Exception as exc:
            print(f"\n❌ Error al conectar con IoT Core: {exc}", file=sys.stderr)
            sys.exit(1)

    # ── Loop ──────────────────────────────────────────────
    try:
        run(
            states     = states,
            fill_model = fill_model,
            publisher  = publisher,
            interval   = args.interval,
            dry_run    = args.dry_run,
        )
    finally:
        if publisher is not None:
            publisher.disconnect()

    print("=" * 58)


if __name__ == "__main__":
    main()
