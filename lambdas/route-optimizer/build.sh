#!/usr/bin/env bash
# build.sh — SmartWaste MVD / route-optimizer
#
# Construye el paquete de despliegue de la Lambda:
#   1. Crea el directorio de build
#   2. Copia handler.py y módulos vendored de cuopt-client/
#   3. Instala deps Python para Linux x86_64 (plataforma de AWS Lambda)
#
# Uso (llamado por Terraform null_resource o manualmente):
#   ./lambdas/route-optimizer/build.sh [BUILD_DIR]
#
# BUILD_DIR default: terraform/.build/route-optimizer
#
# Requiere: pip3, Python 3.11+

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="${1:-$PROJECT_DIR/terraform/.build/route-optimizer}"

echo "==> [route-optimizer] Construyendo paquete Lambda..."
echo "    Origen  : $SCRIPT_DIR"
echo "    Destino : $BUILD_DIR"

# Limpiar y recrear el build dir
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# ── Paso 1: Copiar handler ────────────────────────────────────────────────────
echo "    Copiando handler.py..."
cp "$SCRIPT_DIR/handler.py" "$BUILD_DIR/"

# ── Paso 2: Copiar módulos vendored ──────────────────────────────────────────
echo "    Copiando módulos de cuopt-client (osrm_client, vrp_solver, constraints)..."
cp "$PROJECT_DIR/cuopt-client/osrm_client.py"  "$BUILD_DIR/"
cp "$PROJECT_DIR/cuopt-client/vrp_solver.py"   "$BUILD_DIR/"
cp "$PROJECT_DIR/cuopt-client/constraints.py"  "$BUILD_DIR/"

echo "    Copiando módulos shared (ws_notifier)..."
cp "$PROJECT_DIR/lambdas/shared/ws_notifier.py" "$BUILD_DIR/"

# ── Paso 3: Instalar dependencias para Lambda (Linux x86_64) ─────────────────
# --platform manylinux2014_x86_64: instala wheels para Linux aunque estemos en macOS
# --only-binary=:all:             : solo wheels (no compilar desde source)
# --python-version 3.11           : versión del runtime de Lambda
#
# Si pip no encuentra wheel manylinux para algún paquete, reintentar sin --only-binary.
echo "    Instalando dependencias Python para linux/x86_64..."

pip3 install \
    -r "$SCRIPT_DIR/requirements.txt" \
    -t "$BUILD_DIR/" \
    --platform manylinux2014_x86_64 \
    --only-binary=:all: \
    --python-version 3.11 \
    --implementation cp \
    --quiet \
    --upgrade

echo "    Build completado."
echo "    Archivos en $BUILD_DIR: $(ls "$BUILD_DIR" | wc -l | tr -d ' ') items"
echo "    Tamaño total: $(du -sh "$BUILD_DIR" | cut -f1)"
