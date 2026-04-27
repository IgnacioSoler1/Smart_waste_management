#!/usr/bin/env python3
"""
seed_fill_levels.py — SmartWaste MVD

Rellena fill_level y needs_collection en todos los contenedores de DynamoDB
usando el modelo de llenado FillModel (curva exponencial-saturante).

Simula que los contenedores llevan entre 12 y 72 horas sin ser vaciados,
con variación aleatoria seeded por container_id para reproducibilidad.

Uso:
  python data/scripts/seed_fill_levels.py
  python data/scripts/seed_fill_levels.py --dry-run
  python data/scripts/seed_fill_levels.py --table-name smartwaste-dev-containers
  python data/scripts/seed_fill_levels.py --threshold 40 --hours-range 12 72

Dependencias: boto3, el módulo simulator/ en PYTHONPATH
"""

import argparse
import hashlib
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# Añadir la raíz del proyecto al path para importar simulator/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from simulator.fill_model import FillModel  # noqa: E402

# ─────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────

BATCH_SIZE   = 25
MAX_RETRIES  = 5
DEFAULT_TABLE   = "smartwaste-containers"
DEFAULT_PROFILE = "personal-smart-recycle"
DEFAULT_REGION  = "us-east-1"
NEEDS_COLLECTION_THRESHOLD = 40   # % mínimo para prender la flag


def _hours_for_container(container_id: str, min_h: float, max_h: float) -> float:
    """
    Devuelve horas desde el último vaciado de forma determinista pero
    distribuida uniformemente según el hash del container_id.
    """
    digest = int(hashlib.md5(container_id.encode()).hexdigest(), 16)
    frac = (digest % 10_000) / 10_000.0   # 0.0 .. 0.9999
    return min_h + frac * (max_h - min_h)


def _write_batch(table, items: list[dict]) -> int:
    """Escribe items en DynamoDB usando batch_write_item con reintentos."""
    written = 0
    batch = [{"PutRequest": {"Item": item}} for item in items]

    for attempt in range(MAX_RETRIES):
        resp = table.meta.client.batch_write_item(
            RequestItems={table.name: batch}
        )
        unprocessed = resp.get("UnprocessedItems", {}).get(table.name, [])
        written += len(batch) - len(unprocessed)
        if not unprocessed:
            break
        batch = unprocessed
        delay = 0.5 * (2 ** attempt)
        print(f"  {len(unprocessed)} items sin procesar — reintento en {delay:.1f}s")
        time.sleep(delay)
    else:
        print(f"  WARN: {len(batch)} items no guardados tras {MAX_RETRIES} reintentos")

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed fill_level en contenedores")
    parser.add_argument("--table-name", default=DEFAULT_TABLE)
    parser.add_argument("--region",     default=DEFAULT_REGION)
    parser.add_argument("--profile",    default=DEFAULT_PROFILE)
    parser.add_argument("--threshold",  type=float, default=NEEDS_COLLECTION_THRESHOLD,
                        help="fill_level mínimo para needs_collection=True (default 40)")
    parser.add_argument("--hours-range", nargs=2, type=float, default=[12.0, 72.0],
                        metavar=("MIN", "MAX"),
                        help="Rango de horas desde el último vaciado (default 12 72)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Calcular fill levels sin escribir en DynamoDB")
    args = parser.parse_args()

    min_h, max_h = args.hours_range
    threshold    = args.threshold
    now          = datetime.now(timezone.utc)

    print(f"{'DRY RUN — ' if args.dry_run else ''}Seed fill levels")
    print(f"  Tabla   : {args.table_name}")
    print(f"  Región  : {args.region}")
    print(f"  Perfil  : {args.profile}")
    print(f"  Horas   : {min_h:.0f}–{max_h:.0f}h desde el último vaciado")
    print(f"  Umbral  : {threshold:.0f}% → needs_collection=True")
    print()

    # ── Conexión DynamoDB ─────────────────────────────────
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    dynamodb = session.resource("dynamodb")
    table    = dynamodb.Table(args.table_name)

    # ── Scan completo de contenedores ─────────────────────
    print("Escaneando contenedores...")
    containers: list[dict] = []
    scan_kwargs: dict = {}
    while True:
        resp = table.scan(**scan_kwargs)
        containers.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    print(f"  {len(containers)} contenedores encontrados")
    print()

    # ── Calcular fill levels ──────────────────────────────
    model = FillModel(seed=42)
    batch_items: list[dict] = []
    total_written = 0
    needs_true = 0
    needs_false = 0

    for i, c in enumerate(containers):
        container_id = str(c["container_id"])
        hours = _hours_for_container(container_id, min_h, max_h)
        last_emptied = now - timedelta(hours=hours)

        fill = model.calculate_fill_level(
            container_info={
                "latitude":  float(c.get("latitude",  -34.9)),
                "longitude": float(c.get("longitude", -56.1)),
            },
            current_time=now,
            last_emptied_time=last_emptied,
        )
        fill_rounded = round(fill, 1)
        needs = fill_rounded >= threshold

        if needs:
            needs_true += 1
        else:
            needs_false += 1

        if args.dry_run:
            continue

        # Construir item con todos los campos originales + los nuevos
        item = dict(c)
        item["fill_level"]       = Decimal(str(fill_rounded))
        item["needs_collection"] = needs
        batch_items.append(item)

        if len(batch_items) >= BATCH_SIZE:
            written = _write_batch(table, batch_items)
            total_written += written
            batch_items = []
            if (i + 1) % 500 == 0:
                print(f"  {i + 1}/{len(containers)} procesados...")

    # Flush último batch
    if batch_items and not args.dry_run:
        total_written += _write_batch(table, batch_items)

    # ── Resumen ───────────────────────────────────────────
    print()
    print("=" * 50)
    if args.dry_run:
        print("DRY RUN completado (nada escrito)")
    else:
        print(f"Escritos: {total_written}/{len(containers)} items")
    print(f"needs_collection=True  : {needs_true} ({100*needs_true/len(containers):.1f}%)")
    print(f"needs_collection=False : {needs_false} ({100*needs_false/len(containers):.1f}%)")
    print("=" * 50)


if __name__ == "__main__":
    main()
