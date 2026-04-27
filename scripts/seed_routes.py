#!/usr/bin/env python3
"""
seed_routes.py — SmartWaste MVD

Genera rutas optimizadas para todos los circuitos con datos de prueba.

Modos de uso:
  1. Solo invocar (no toca DynamoDB, solo optimiza circuitos que ya califican):
       python scripts/seed_routes.py --invoke-only

  2. Simular fill levels + invocar (para tener datos de comparación completos):
       python scripts/seed_routes.py

  3. Ver estadísticas de qué circuitos calificarían sin hacer nada:
       python scripts/seed_routes.py --dry-run

Opciones:
  --env          Prefijo de tablas DynamoDB (default: smartwaste-dev)
  --region       AWS region (default: us-east-1)
  --profile      AWS profile (default: personal-smart-recycle)
  --fill-pct     Porcentaje de fill para los contenedores simulados (default: 75)
  --fraction     Fracción de contenedores del circuito que se llevan a --fill-pct (default: 0.6)
  --min-containers  Mínimo de contenedores >60% para optimizar (debe coincidir con Lambda, default: 5)
  --delay        Segundos entre invocaciones Lambda (default: 2)
  --invoke-only  No modifica DynamoDB, solo invoca la Lambda para circuitos que ya califican
  --dry-run      Solo muestra estadísticas, no modifica nada ni invoca Lambda
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed fill levels y genera rutas optimizadas")
    p.add_argument("--env",             default="smartwaste-dev")
    p.add_argument("--region",          default="us-east-1")
    p.add_argument("--profile",         default="personal-smart-recycle")
    p.add_argument("--fill-pct",        type=int,   default=75,
                   help="Fill level simulado para contenedores seleccionados (default: 75)")
    p.add_argument("--fraction",        type=float, default=0.6,
                   help="Fracción de contenedores por circuito a subir (default: 0.6 = 60%%)")
    p.add_argument("--min-containers",  type=int,   default=5,
                   help="Mismo umbral que la Lambda (default: 5)")
    p.add_argument("--delay",           type=float, default=2.0,
                   help="Segundos entre invocaciones Lambda (default: 2)")
    p.add_argument("--invoke-only",     action="store_true",
                   help="No modifica DynamoDB, solo invoca circuitos que ya califican")
    p.add_argument("--dry-run",         action="store_true",
                   help="Solo estadísticas, sin modificar ni invocar")
    return p.parse_args()


# ─────────────────────────────────────────────────────────
# DynamoDB helpers
# ─────────────────────────────────────────────────────────

def scan_all(table, **kwargs) -> list[dict]:
    """Scan completo con paginación."""
    items: list[dict] = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def load_containers(table) -> dict[str, list[dict]]:
    """Retorna {circuit_id: [container, ...]} para todos los activos."""
    print("Leyendo contenedores de DynamoDB...")
    items = scan_all(
        table,
        FilterExpression="status = :s",
        ExpressionAttributeValues={":s": "active"},
        ProjectionExpression="container_id, circuit_id, fill_level, needs_collection, shift",
    )
    by_circuit: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        cid = str(item.get("circuit_id", ""))
        if cid:
            by_circuit[cid].append(item)
    print(f"  {len(items):,} contenedores activos en {len(by_circuit)} circuitos")
    return dict(by_circuit)


# ─────────────────────────────────────────────────────────
# Estadísticas
# ─────────────────────────────────────────────────────────

def print_stats(by_circuit: dict[str, list[dict]], min_containers: int) -> tuple[list[str], list[str]]:
    """
    Imprime estadísticas por circuito.
    Retorna (qualifying_circuits, non_qualifying_circuits).
    """
    qualifying: list[str] = []
    non_qualifying: list[str] = []

    print(f"\n{'─'*70}")
    print(f"{'CIRCUITO':<25} {'TOTAL':>6} {'> 60%':>6} {'CALIFICA':>9} {'TURNO':>8}")
    print(f"{'─'*70}")

    for cid in sorted(by_circuit.keys()):
        containers = by_circuit[cid]
        total = len(containers)
        mandatory = sum(1 for c in containers if float(c.get("fill_level", 0)) > 60)
        shift = containers[0].get("shift", "?") if containers else "?"
        qualifies = mandatory >= min_containers
        mark = "SI" if qualifies else "no"
        if qualifies:
            qualifying.append(cid)
        else:
            non_qualifying.append(cid)
        print(f"  {cid:<23} {total:>6} {mandatory:>6}   {mark:>8}   {shift:>6}")

    print(f"{'─'*70}")
    print(f"  Califican: {len(qualifying)} / {len(by_circuit)} circuitos "
          f"(threshold: ≥{min_containers} contenedores con fill > 60%)\n")

    return qualifying, non_qualifying


# ─────────────────────────────────────────────────────────
# Seed fill levels
# ─────────────────────────────────────────────────────────

def seed_fill_levels(
    table,
    by_circuit: dict[str, list[dict]],
    non_qualifying: list[str],
    fill_pct: int,
    fraction: float,
) -> list[str]:
    """
    Para los circuitos que no califican, sube fill_level en `fraction` de sus
    contenedores a `fill_pct` y setea needs_collection=True.

    Retorna la lista de circuit_ids que ahora califican.
    """
    print(f"Actualizando fill levels en {len(non_qualifying)} circuitos no calificados...")
    print(f"  Parámetros: fill={fill_pct}%, fracción={fraction*100:.0f}% de contenedores por circuito")

    now_updated: list[str] = []
    random.seed(42)  # reproducible

    with table.batch_writer() as batch:
        for cid in non_qualifying:
            containers = by_circuit[cid]
            n_to_fill  = max(5, int(len(containers) * fraction))  # al menos 5
            # Elegir aleatoriamente (evitar los que ya están altos)
            candidates = [c for c in containers if float(c.get("fill_level", 0)) <= 60]
            selected   = random.sample(candidates, min(n_to_fill, len(candidates)))

            for c in selected:
                # Usamos put_item con todos los campos del ítem existente para no perder datos.
                # Como solo tenemos proyección parcial, hacemos update_item selectivo.
                table.update_item(
                    Key={"container_id": c["container_id"]},
                    UpdateExpression="SET fill_level = :fl, needs_collection = :nc",
                    ExpressionAttributeValues={
                        ":fl": Decimal(str(fill_pct)),
                        ":nc": True,
                    },
                )

            now_updated.append(cid)

    print(f"  Actualizados: {len(now_updated)} circuitos\n")
    return now_updated


# ─────────────────────────────────────────────────────────
# Invocar Lambda
# ─────────────────────────────────────────────────────────

def invoke_optimizer(lambda_client, function_name: str, circuit_id: str) -> dict:
    """Invoca la Lambda de optimización de forma síncrona y retorna el resultado."""
    payload = json.dumps({"circuit_id": circuit_id})
    resp = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=payload.encode(),
    )
    body = json.loads(resp["Payload"].read())
    return body


def run_optimizations(
    lambda_client,
    function_name: str,
    circuit_ids: list[str],
    delay: float,
) -> None:
    """Invoca la Lambda secuencialmente para cada circuito."""
    total = len(circuit_ids)
    print(f"Invocando route-optimizer para {total} circuitos (delay={delay}s entre cada uno)...")
    print(f"  Función: {function_name}\n")

    results: dict[str, int] = {"optimized": 0, "skipped": 0, "error": 0}

    for i, cid in enumerate(circuit_ids, 1):
        print(f"  [{i:>3}/{total}] {cid:<30}", end=" ", flush=True)
        try:
            result = invoke_optimizer(lambda_client, function_name, cid)

            # El resultado puede ser un dict directo o envuelto en statusCode/body
            if "statusCode" in result:
                body = json.loads(result.get("body", "{}")) if isinstance(result.get("body"), str) else result.get("body", {})
            else:
                body = result

            # Buscar el resumen del circuito en la respuesta
            circuits_result = body.get("circuits", [body])
            circuit_result  = next((c for c in circuits_result if c.get("circuit_id") == cid), circuits_result[0] if circuits_result else {})

            status = circuit_result.get("status", "unknown")

            if status == "optimized":
                saved = circuit_result.get("routes_saved", [])
                stops = sum(r.get("stops", 0) for r in saved)
                dist  = sum(r.get("distance_m", 0) for r in saved) / 1000
                print(f"OK  {len(saved)} truck(s), {stops} paradas, {dist:.1f} km")
                results["optimized"] += 1
            elif status == "skipped":
                reason = circuit_result.get("reason", "")
                mandatory = circuit_result.get("containers", "?")
                print(f"skip  ({mandatory} contenedores, {reason})")
                results["skipped"] += 1
            else:
                print(f"ERROR  {circuit_result.get('error', status)}")
                results["error"] += 1

        except Exception as exc:
            print(f"EXCEPCION  {exc}")
            results["error"] += 1

        if i < total:
            time.sleep(delay)

    print(f"\n{'─'*60}")
    print(f"Resultado final:")
    print(f"  Optimizados: {results['optimized']}")
    print(f"  Saltados:    {results['skipped']}  (fill insuficiente)")
    print(f"  Errores:     {results['error']}")
    print(f"{'─'*60}\n")


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    dynamodb = session.resource("dynamodb")
    lambda_client = session.client("lambda")

    containers_table  = dynamodb.Table(f"{args.env}-containers")
    function_name     = f"{args.env}-route-optimizer"

    # 1. Cargar contenedores
    by_circuit = load_containers(containers_table)

    # 2. Estadísticas actuales
    qualifying, non_qualifying = print_stats(by_circuit, args.min_containers)

    if args.dry_run:
        print("Modo --dry-run: sin modificaciones ni invocaciones.")
        sys.exit(0)

    circuits_to_optimize: list[str] = list(qualifying)

    # 3. (Opcional) Seed fill levels para los circuitos que no califican
    if not args.invoke_only and non_qualifying:
        print(f"Se van a subir fill levels en {len(non_qualifying)} circuitos para testing.")
        confirm = input("Continuar? [s/N] ").strip().lower()
        if confirm != "s":
            print("Abortado. Usá --invoke-only para solo optimizar los que ya califican.")
            sys.exit(0)
        seed_fill_levels(
            containers_table,
            by_circuit,
            non_qualifying,
            fill_pct=args.fill_pct,
            fraction=args.fraction,
        )
        circuits_to_optimize = sorted(by_circuit.keys())  # ahora todos deberían calificar

    if not circuits_to_optimize:
        print("No hay circuitos para optimizar.")
        sys.exit(0)

    # 4. Invocar Lambda secuencialmente
    run_optimizations(lambda_client, function_name, circuits_to_optimize, args.delay)


if __name__ == "__main__":
    main()
