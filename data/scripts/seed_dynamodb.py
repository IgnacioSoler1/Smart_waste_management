#!/usr/bin/env python3
"""
seed_dynamodb.py — SmartWaste MVD

Carga los contenedores procesados desde containers_enriched.json
a la tabla DynamoDB `smartwaste-containers`.

Uso:
  python data/scripts/seed_dynamodb.py
  python data/scripts/seed_dynamodb.py --dry-run
  python data/scripts/seed_dynamodb.py --table-name smartwaste-dev-containers
  python data/scripts/seed_dynamodb.py --table-name smartwaste-dev-containers \\
      --region us-east-1 --profile myprofile

Dependencias: boto3
"""

import argparse
import json
import math
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

# ─────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────

BATCH_SIZE = 25          # Límite hard de DynamoDB para batch_write_item
MAX_RETRIES = 5          # Reintentos máximos para UnprocessedItems
RETRY_BASE_DELAY = 0.5   # Segundos — se duplica en cada reintento (backoff exponencial)
DEFAULT_TABLE = "smartwaste-containers"
DEFAULT_PROFILE = "personal-smart-recycle"
DEFAULT_INPUT = "data/processed/containers_enriched.json"
DEFAULT_CAPACITY_LITERS = 2400

# Datos completos de los depots (misma fuente que consolidate_data.py)
DEPOTS: dict[str, dict[str, Any]] = {
    "depot_estacion_transferencia": {
        "name": "Estación de Transferencia (Ruta 102)",
        "latitude": -34.8128,
        "longitude": -56.2645,
    },
    "depot_felipe_cardoso": {
        "name": "Sitio Disposición Final Felipe Cardoso",
        "latitude": -34.8347,
        "longitude": -56.0967,
    },
}


# ─────────────────────────────────────────────────────────
# Construcción del item DynamoDB
# ─────────────────────────────────────────────────────────

def to_decimal(value: float | int | str) -> Decimal:
    """
    Convierte un número a Decimal de forma segura.

    DynamoDB no acepta float de Python. Pasamos por str para preservar
    la precisión original y evitar el ruido binario de IEEE 754
    (e.g. Decimal(0.1) → 0.1000000000000000055511... vs Decimal("0.1") → 0.1).
    """
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"No se puede convertir a Decimal: {value!r}") from exc


def build_dynamo_item(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Transforma un contenedor de containers_enriched.json al esquema
    de la tabla DynamoDB.

    Esquema resultante:
      container_id    (S)  PK
      circuit_id      (S)  GSI circuit-index PK
      latitude        (N)  WGS84
      longitude       (N)  WGS84
      zone            (S)  'east' | 'west'
      depot_name      (S)  nombre del depot asignado al circuito
      depot_lat       (N)  coordenada del depot
      depot_lon       (N)
      shift           (S)  'morning' | 'afternoon' | 'night' | 'UNKNOWN'
      collection_days (L)  lista de strings, e.g. ["LUNES", "MIERCOLES"]
      status          (S)  'active'
      capacity_liters (N)  litros, default 2400
      fill_level      (N)  0–100, inicializado en 0
      last_emptied    (NULL)  se actualiza con cada vaciado real/simulado
      fill_updated_at (NULL)  timestamp de la última lectura del sensor
    """
    depot_id = raw.get("depot_id", "")
    depot = DEPOTS.get(depot_id, {"name": depot_id, "latitude": 0.0, "longitude": 0.0})

    collection_days: list[str] = raw.get("collection_days") or []

    item = {
        "container_id":    str(raw["container_id"]),
        "circuit_id":      str(raw["circuit_id"]),
        "latitude":        to_decimal(raw["latitude"]),
        "longitude":       to_decimal(raw["longitude"]),
        "zone":            str(raw.get("zone", "")),
        "depot_name":      str(depot["name"]),
        "depot_lat":       to_decimal(depot["latitude"]),
        "depot_lon":       to_decimal(depot["longitude"]),
        "shift":           str(raw.get("shift", "UNKNOWN")),
        "collection_days": [str(d) for d in collection_days],
        "status":          str(raw.get("status", "active")),
        "capacity_liters": Decimal(DEFAULT_CAPACITY_LITERS),
        "fill_level":      Decimal("0"),
        "last_emptied":    None,   # → DynamoDB NULL
        "fill_updated_at": None,   # → DynamoDB NULL
    }

    if "csv_sequence" in raw:
        item["csv_sequence"] = Decimal(str(raw["csv_sequence"]))

    return item


# ─────────────────────────────────────────────────────────
# Serialización al formato de wire DynamoDB
# ─────────────────────────────────────────────────────────

_serializer = TypeSerializer()


def serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Convierte un dict Python a formato tipado DynamoDB.

    TypeSerializer maneja:
      str     → {"S": ...}
      Decimal → {"N": "..."} (string, como requiere DynamoDB)
      list    → {"L": [...]}
      None    → {"NULL": True}
      bool    → {"BOOL": ...}

    Ejemplo de salida:
      {"container_id": {"S": "101941"}, "latitude": {"N": "-34.835566"}, ...}
    """
    return {key: _serializer.serialize(value) for key, value in item.items()}


# ─────────────────────────────────────────────────────────
# Batching y reintentos
# ─────────────────────────────────────────────────────────

def chunked(lst: list, size: int):
    """Divide una lista en sublistas de hasta `size` elementos."""
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def write_batch_with_retry(
    client,
    table_name: str,
    serialized_items: list[dict[str, Any]],
    retry_stats: dict[str, int],
) -> None:
    """
    Envía un batch de hasta 25 items a DynamoDB con reintentos.

    DynamoDB puede devolver UnprocessedItems cuando hay throttling
    puntual o picos de escritura. La estrategia de backoff exponencial
    (0.5s, 1s, 2s, 4s, 8s) da tiempo al servicio para recuperarse.

    Args:
        client:           boto3 DynamoDB client (baja latencia vs resource)
        table_name:       nombre de la tabla destino
        serialized_items: items ya tipados en formato DynamoDB wire
        retry_stats:      dict mutable para acumular contadores globales

    Raises:
        RuntimeError: si quedan UnprocessedItems tras MAX_RETRIES intentos.
    """
    put_requests = [{"PutRequest": {"Item": item}} for item in serialized_items]

    for attempt in range(MAX_RETRIES + 1):
        response = client.batch_write_item(
            RequestItems={table_name: put_requests}
        )
        unprocessed = response.get("UnprocessedItems", {}).get(table_name, [])

        if not unprocessed:
            return  # ✓ Todos los items escritos

        retry_stats["retries"] += 1
        retry_stats["unprocessed_total"] += len(unprocessed)

        if attempt == MAX_RETRIES:
            raise RuntimeError(
                f"Quedaron {len(unprocessed)} UnprocessedItems después de "
                f"{MAX_RETRIES} reintentos en '{table_name}'."
            )

        delay = RETRY_BASE_DELAY * (2 ** attempt)
        time.sleep(delay)
        put_requests = unprocessed   # Siguiente intento solo con los fallidos


# ─────────────────────────────────────────────────────────
# Barra de progreso
# ─────────────────────────────────────────────────────────

def _progress_bar(current: int, total: int, bar_width: int = 36) -> str:
    pct = current / total if total > 0 else 0
    filled = math.floor(pct * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)
    return f"[{bar}] {current:>6,}/{total:,}  {pct * 100:5.1f}%"


def print_progress(
    current: int,
    total: int,
    batches_done: int,
    retry_stats: dict[str, int],
    elapsed: float,
) -> None:
    line = (
        "  "
        + _progress_bar(current, total)
        + f"  batch {batches_done:>4}"
        + f"  retries {retry_stats['retries']}"
        + f"  {elapsed:.1f}s"
    )
    sys.stdout.write("\r" + line.ljust(95))
    sys.stdout.flush()


# ─────────────────────────────────────────────────────────
# Dry-run
# ─────────────────────────────────────────────────────────

def dry_run_preview(raw_containers: list[dict[str, Any]], table_name: str) -> None:
    total = len(raw_containers)
    total_batches = math.ceil(total / BATCH_SIZE)
    print(f"\n  [DRY-RUN] Se escribirían {total:,} items en '{table_name}'.")
    print(f"  [DRY-RUN] Se usarían {total_batches} batches de hasta {BATCH_SIZE} items.\n")

    preview_count = min(3, total)
    print(f"  Muestra de {preview_count} item(s):")
    print("  " + "─" * 62)

    for raw in raw_containers[:preview_count]:
        item = build_dynamo_item(raw)
        for field, value in item.items():
            suffix = "  → NULL en DynamoDB" if value is None else ""
            print(f"    {field:<20} = {value!r}{suffix}")
        print("  " + "─" * 62)


# ─────────────────────────────────────────────────────────
# Orquestación principal
# ─────────────────────────────────────────────────────────

def seed(
    raw_containers: list[dict[str, Any]],
    table_name: str,
    client,
    dry_run: bool,
) -> None:
    """
    Serializa todos los items y los escribe en DynamoDB en batches de 25.

    La serialización se hace en bloque antes de empezar a escribir para
    detectar errores de datos rápido (fail-fast) y separar la lógica
    de transformación de la de I/O de red.
    """
    total = len(raw_containers)
    retry_stats = {"retries": 0, "unprocessed_total": 0}
    start_time = time.monotonic()

    print(f"\n  Serializando {total:,} contenedores...")
    try:
        serialized = [serialize_item(build_dynamo_item(r)) for r in raw_containers]
    except (KeyError, ValueError) as exc:
        print(f"\n❌ Error construyendo item DynamoDB: {exc}", file=sys.stderr)
        sys.exit(1)

    action = "[DRY-RUN] Simulando escritura" if dry_run else "Escribiendo"
    print(f"  {action} en la tabla '{table_name}'...\n")

    items_done = 0
    batches_done = 0

    for batch in chunked(serialized, BATCH_SIZE):
        if not dry_run:
            try:
                write_batch_with_retry(client, table_name, batch, retry_stats)
            except (ClientError, RuntimeError) as exc:
                print(f"\n\n❌ Error en batch {batches_done + 1}: {exc}", file=sys.stderr)
                sys.exit(1)

        items_done += len(batch)
        batches_done += 1
        elapsed = time.monotonic() - start_time
        print_progress(items_done, total, batches_done, retry_stats, elapsed)

    elapsed = time.monotonic() - start_time
    print_progress(total, total, batches_done, retry_stats, elapsed)
    print()  # Nueva línea tras la barra

    # ── Estadísticas finales ──────────────────────────────
    throughput = items_done / elapsed if elapsed > 0 else 0
    print()
    print("  " + "─" * 50)
    if dry_run:
        print("  [DRY-RUN] No se escribió nada en DynamoDB.")
    else:
        print("  ✅ Carga completada.")
    print(f"  Items procesados:    {items_done:>8,}")
    print(f"  Batches enviados:    {batches_done:>8,}")
    print(f"  Reintentos totales:  {retry_stats['retries']:>8,}")
    if retry_stats["unprocessed_total"] > 0:
        print(f"  Items reintentados:  {retry_stats['unprocessed_total']:>8,}")
    print(f"  Tiempo total:        {elapsed:>7.1f}s")
    if not dry_run:
        print(f"  Throughput:          {throughput:>6.0f} items/s")
    print("  " + "─" * 50)


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Carga containers_enriched.json a la tabla DynamoDB de contenedores SmartWaste."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  # Ver qué haría sin escribir nada
  python data/scripts/seed_dynamodb.py --dry-run

  # Carga real en entorno dev (perfil AWS nombrado)
  python data/scripts/seed_dynamodb.py \\
      --table-name smartwaste-dev-containers \\
      --profile personal-smart-recycle

  # Carga en producción
  python data/scripts/seed_dynamodb.py \\
      --table-name smartwaste-containers \\
      --region us-east-1
        """,
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help=f"Path al JSON de entrada (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--table-name",
        default=DEFAULT_TABLE,
        dest="table_name",
        help=f"Nombre de la tabla DynamoDB destino (default: {DEFAULT_TABLE})",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="Región AWS (default: us-east-1)",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"Perfil AWS CLI (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Muestra qué se haría sin escribir nada en DynamoDB",
    )
    args = parser.parse_args()

    print("=" * 57)
    print("  SmartWaste MVD — Seed DynamoDB › contenedores")
    print("=" * 57)

    # ── Cargar JSON ───────────────────────────────────────
    input_path = Path(args.input)
    print(f"\n  Leyendo: {input_path}")
    if not input_path.exists():
        print(f"❌ Archivo no encontrado: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, encoding="utf-8") as f:
        raw_containers: list[dict[str, Any]] = json.load(f)

    if not isinstance(raw_containers, list):
        print(
            f"❌ Se esperaba una lista JSON, se recibió {type(raw_containers).__name__}.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"  Contenedores cargados: {len(raw_containers):,}")

    # ── Dry-run: salir después del preview ───────────────
    if args.dry_run:
        dry_run_preview(raw_containers, args.table_name)
        seed(raw_containers, args.table_name, client=None, dry_run=True)
        print()
        return

    # ── Crear cliente y verificar tabla ──────────────────
    session_kwargs: dict[str, Any] = {"region_name": args.region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile

    try:
        session = boto3.Session(**session_kwargs)
        client = session.client("dynamodb")
        # Verificar que la tabla existe antes de empezar
        client.describe_table(TableName=args.table_name)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            print(
                f"\n❌ La tabla '{args.table_name}' no existe en {args.region}.\n"
                "   Ejecutá 'terraform apply' primero para crearla.",
                file=sys.stderr,
            )
        elif code in ("ExpiredTokenException", "InvalidClientTokenId"):
            print(
                f"\n❌ Credenciales AWS inválidas o expiradas ({code}).\n"
                "   Configurá aws-cli o usá --profile.",
                file=sys.stderr,
            )
        else:
            print(f"\n❌ Error al conectar con DynamoDB: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\n❌ Error al inicializar el cliente AWS: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"\n  Tabla verificada: '{args.table_name}' ({args.region})")
    if args.profile:
        print(f"  Perfil AWS:       {args.profile}")

    # ── Escribir ──────────────────────────────────────────
    seed(raw_containers, args.table_name, client, dry_run=False)

    print()
    print("  Verificar resultado:")
    print(f"    aws dynamodb scan --table-name {args.table_name} --select COUNT")
    print("=" * 57)


if __name__ == "__main__":
    main()
