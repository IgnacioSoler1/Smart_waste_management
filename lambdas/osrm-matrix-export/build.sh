#!/usr/bin/env bash
# build.sh — SmartWaste MVD / osrm-matrix-export
#
# Construye el paquete de la Lambda de exportación de matrices OSRM:
#   1. Copia handler.py y osrm_client.py (cuopt-client/)
#   2. Copia los datos de contenedores y depósitos (data/processed/)
#   3. Instala requests para Linux x86_64
#
# Uso: ./lambdas/osrm-matrix-export/build.sh [BUILD_DIR]
# BUILD_DIR default: terraform/.build/osrm-matrix-export

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="${1:-$PROJECT_DIR/terraform/.build/osrm-matrix-export}"

echo "==> [osrm-matrix-export] Construyendo paquete Lambda en $BUILD_DIR"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/data"

cp "$SCRIPT_DIR/handler.py"                              "$BUILD_DIR/"
cp "$PROJECT_DIR/cuopt-client/osrm_client.py"            "$BUILD_DIR/"
cp "$PROJECT_DIR/data/processed/containers_enriched.json" "$BUILD_DIR/data/"
cp "$PROJECT_DIR/data/processed/fixed_locations.json"    "$BUILD_DIR/data/"

pip3 install \
    -r "$SCRIPT_DIR/requirements.txt" \
    -t "$BUILD_DIR/" \
    --platform manylinux2014_x86_64 \
    --only-binary=:all: \
    --python-version 3.11 \
    --implementation cp \
    --quiet \
    --upgrade

echo "    Build completado: $(du -sh "$BUILD_DIR" | cut -f1)"
