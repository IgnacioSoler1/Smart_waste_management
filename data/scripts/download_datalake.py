#!/usr/bin/env python3
"""
download_datalake.py — SmartWaste MVD

Descarga archivos del S3 data lake preservando la jerarquía de carpetas.

Uso:
  # Descargar las tres carpetas por defecto
  python download_datalake.py

  # Carpeta de destino personalizada
  python download_datalake.py --output-dir /tmp/datalake

  # Solo algunas carpetas
  python download_datalake.py --prefixes analytics-results/ route-results-parquet/

  # Forzar re-descarga aunque el archivo ya exista localmente
  python download_datalake.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BUCKET = "smartwaste-data-lake-dev"
AWS_PROFILE = "personal-classify"
REGION = "us-east-1"

DEFAULT_PREFIXES = [
    "analytics-results/",
    "route-results-parquet/",
    "sensor-readings-parquet/",
]

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "raw" / "datalake"


def sizeof_fmt(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def list_objects(s3, prefix: str) -> list[dict]:
    paginator = s3.get_paginator("list_objects_v2")
    objects: list[dict] = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/"):  # skip directory markers
                objects.append(obj)
    return objects


def download_prefix(
    s3,
    prefix: str,
    output_dir: Path,
    force: bool,
) -> tuple[int, int, int]:
    """Returns (downloaded, skipped, failed)."""
    objects = list_objects(s3, prefix)
    if not objects:
        print(f"  [WARN] No objects found under s3://{BUCKET}/{prefix}")
        return 0, 0, 0

    total_size = sum(o["Size"] for o in objects)
    print(f"  {len(objects)} objects ({sizeof_fmt(total_size)})")

    downloaded = skipped = failed = 0

    for obj in objects:
        key = obj["Key"]
        local_path = output_dir / key

        if local_path.exists() and not force:
            skipped += 1
            continue

        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            s3.download_file(BUCKET, key, str(local_path))
            downloaded += 1
            print(f"    ✓ {key}  ({sizeof_fmt(obj['Size'])})")
        except ClientError as exc:
            failed += 1
            print(f"    ✗ {key}  ERROR: {exc}", file=sys.stderr)

    return downloaded, skipped, failed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Descarga archivos del S3 data lake de SmartWaste MVD",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--prefixes",
        nargs="+",
        default=DEFAULT_PREFIXES,
        metavar="PREFIX",
        help=f"Prefijos S3 a descargar (default: {DEFAULT_PREFIXES})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directorio de destino (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-descargar aunque el archivo ya exista localmente",
    )
    parser.add_argument(
        "--bucket",
        default=BUCKET,
        help=f"Bucket S3 (default: {BUCKET})",
    )
    parser.add_argument(
        "--profile",
        default=AWS_PROFILE,
        help=f"AWS profile (default: {AWS_PROFILE})",
    )
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    s3 = session.client("s3")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Bucket:  s3://{args.bucket}")
    print(f"Destino: {args.output_dir}")
    print(f"Forzar:  {args.force}")
    print()

    total_dl = total_skip = total_fail = 0

    for prefix in args.prefixes:
        print(f"→ {prefix}")
        dl, skip, fail = download_prefix(s3, prefix, args.output_dir, args.force)
        total_dl += dl
        total_skip += skip
        total_fail += fail
        print(f"    descargados={dl}  ya existían={skip}  errores={fail}")
        print()

    print("─" * 50)
    print(f"Total descargados : {total_dl}")
    print(f"Total ya existían : {total_skip}  (usa --force para re-descargar)")
    print(f"Total errores     : {total_fail}")

    if total_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
