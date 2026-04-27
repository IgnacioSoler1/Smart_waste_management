#!/usr/bin/env python3
"""
backfill_routes_to_firehose.py — SmartWaste MVD

Lee todas las rutas existentes en DynamoDB y las publica a Kinesis Firehose
para poblar el data lake S3 con datos históricos.

Útil cuando el Firehose se desplegó después de que ya había rutas en DynamoDB.

Uso:
    python scripts/backfill_routes_to_firehose.py

Opciones:
    --env       Prefijo de recursos (default: smartwaste-dev)
    --region    AWS region (default: us-east-1)
    --profile   AWS profile (default: personal-smart-recycle)
    --dry-run   Muestra cuántos registros se enviarían sin enviar nada
    --status    Filtro de status: active | superseded | all (default: all)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal

import boto3


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill rutas de DynamoDB a Kinesis Firehose")
    p.add_argument("--env",     default="smartwaste-dev")
    p.add_argument("--region",  default="us-east-1")
    p.add_argument("--profile", default="personal-smart-recycle")
    p.add_argument("--dry-run", action="store_true",
                   help="No envía nada, solo muestra estadísticas")
    p.add_argument("--status",  default="all",
                   choices=["active", "superseded", "all"],
                   help="Filtro de status de rutas (default: all)")
    return p.parse_args()


# ─────────────────────────────────────────────────────────
# DynamoDB helpers
# ─────────────────────────────────────────────────────────

def scan_routes(table, status_filter: str) -> list[dict]:
    """Escanea la tabla de rutas con paginación."""
    print(f"Escaneando rutas (status={status_filter})...")

    kwargs: dict = {}
    if status_filter != "all":
        kwargs["FilterExpression"] = "#s = :s"
        kwargs["ExpressionAttributeNames"] = {"#s": "status"}
        kwargs["ExpressionAttributeValues"] = {":s": status_filter}

    items: list[dict] = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    print(f"  {len(items):,} rutas encontradas")
    return items


def _to_float(val) -> float:
    """Convierte Decimal o cualquier numérico a float."""
    if isinstance(val, Decimal):
        return float(val)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _to_int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def build_firehose_record(item: dict) -> dict | None:
    """
    Construye el mismo JSON compacto que route-optimizer escribe a Firehose.
    Devuelve None si el item no tiene los campos mínimos requeridos.
    """
    route_id   = str(item.get("route_id", ""))
    circuit_id = str(item.get("circuit_id", ""))
    if not route_id or not circuit_id:
        return None

    # Extraer fecha desde created_at o updated_at
    created_at = str(item.get("created_at", ""))
    date_str = created_at[:10] if created_at else "unknown"

    return {
        "date":                     date_str,
        "circuit_id":               circuit_id,
        "route_id":                 route_id,
        "truck_id":                 str(item.get("truck_id", "")),
        "created_at":               created_at,
        "baseline_distance_m":      _to_int(item.get("baseline_distance_m", 0)),
        "total_distance_m":         _to_int(item.get("total_distance_m", 0)),
        "baseline_duration_s":      _to_int(item.get("baseline_duration_s", 0)),
        "total_duration_s":         _to_int(item.get("total_duration_s", 0)),
        "baseline_stops":           _to_int(item.get("baseline_stops", 0)),
        "optimized_stops":          _to_int(
            len(item.get("stops", [])) if isinstance(item.get("stops"), list)
            else item.get("optimized_stops", 0)
        ),
        "stops_skipped":            max(0, _to_int(item.get("baseline_stops", 0)) -
                                        _to_int(
                                            len(item.get("stops", [])) if isinstance(item.get("stops"), list)
                                            else item.get("optimized_stops", 0)
                                        )),
        "distance_improvement_pct": _to_float(item.get("distance_improvement_pct", 0)),
        "duration_improvement_pct": _to_float(item.get("duration_improvement_pct", 0)),
        "solver":                   str(item.get("solver", "")),
        "solver_status":            str(item.get("solver_status", "")),
    }


# ─────────────────────────────────────────────────────────
# Firehose — put_record_batch (hasta 500 records / 4 MB por llamada)
# ─────────────────────────────────────────────────────────

BATCH_SIZE = 400  # conservador — cada record es ~500 B, 400 × 500 B = 200 KB


def send_to_firehose(
    firehose_client,
    stream_name: str,
    records: list[dict],
    dry_run: bool,
) -> tuple[int, int]:
    """
    Envía records en batches a Firehose.
    Retorna (enviados, fallidos).
    """
    sent = 0
    failed = 0
    total = len(records)

    for batch_start in range(0, total, BATCH_SIZE):
        batch = records[batch_start : batch_start + BATCH_SIZE]

        firehose_records = [
            {"Data": (json.dumps(r, ensure_ascii=False) + "\n").encode()}
            for r in batch
        ]

        batch_end = min(batch_start + BATCH_SIZE, total)
        print(f"  Enviando records {batch_start + 1}-{batch_end} / {total}...", end=" ", flush=True)

        if dry_run:
            print("(dry-run, omitido)")
            sent += len(batch)
            continue

        try:
            resp = firehose_client.put_record_batch(
                DeliveryStreamName=stream_name,
                Records=firehose_records,
            )
            batch_failed = resp.get("FailedPutCount", 0)
            batch_sent   = len(batch) - batch_failed
            sent   += batch_sent
            failed += batch_failed

            if batch_failed > 0:
                print(f"OK ({batch_sent} enviados, {batch_failed} FALLARON)")
            else:
                print(f"OK ({batch_sent} enviados)")

        except Exception as exc:
            print(f"ERROR: {exc}")
            failed += len(batch)

        # Pausa breve entre batches para no saturar el stream
        if batch_end < total:
            time.sleep(0.5)

    return sent, failed


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    dynamodb = session.resource("dynamodb")
    firehose = session.client("firehose")

    routes_table  = dynamodb.Table(f"{args.env}-routes")
    stream_name   = f"{args.env}-route-firehose"

    # 1. Escanear DynamoDB
    items = scan_routes(routes_table, args.status)

    if not items:
        print("No hay rutas para backfill.")
        sys.exit(0)

    # 2. Construir records Firehose
    records: list[dict] = []
    skipped = 0
    for item in items:
        record = build_firehose_record(item)
        if record:
            records.append(record)
        else:
            skipped += 1

    print(f"\n  {len(records):,} records listos para enviar")
    if skipped:
        print(f"  {skipped} items omitidos (sin route_id o circuit_id)")

    # Distribución por fecha
    from collections import Counter
    date_counts = Counter(r["date"] for r in records)
    print("\n  Distribución por fecha:")
    for date, count in sorted(date_counts.items()):
        print(f"    {date}: {count} rutas")

    if args.dry_run:
        print("\nModo --dry-run: sin envíos.")
        sys.exit(0)

    # 3. Confirmar envío
    print(f"\nSe van a enviar {len(records):,} records a Firehose '{stream_name}'.")
    confirm = input("Continuar? [s/N] ").strip().lower()
    if confirm != "s":
        print("Abortado.")
        sys.exit(0)

    # 4. Enviar a Firehose
    print()
    sent, failed = send_to_firehose(firehose, stream_name, records, dry_run=False)

    print(f"\n{'─'*50}")
    print(f"Backfill completado:")
    print(f"  Enviados: {sent:,}")
    print(f"  Fallidos: {failed:,}")
    print(f"\nNota: Firehose bufferiza hasta 15 min antes de escribir a S3.")
    print(f"Verificar en: s3://smartwaste-data-lake-dev/route-results/")
    print(f"{'─'*50}")


if __name__ == "__main__":
    main()
