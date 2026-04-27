#!/usr/bin/env bash
# build.sh — SmartWaste MVD / sensor-simulator
#
# Construye el paquete de despliegue de la Lambda sensor-simulator.
# Copia handler.py + módulos del simulador (fill_model, zone_density).
# No necesita dependencias externas (solo boto3 que viene en Lambda).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="${1:-$PROJECT_DIR/terraform/.build/sensor-simulator}"

echo "==> [sensor-simulator] Construyendo paquete Lambda..."
echo "    Origen  : $SCRIPT_DIR"
echo "    Destino : $BUILD_DIR"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "    Copiando handler.py..."
cp "$SCRIPT_DIR/handler.py" "$BUILD_DIR/"

echo "    Copiando módulos del simulador (fill_model, zone_density)..."
cp "$PROJECT_DIR/simulator/fill_model.py" "$BUILD_DIR/"
cp "$PROJECT_DIR/simulator/zone_density.py" "$BUILD_DIR/"

# fix: fill_model.py importa 'from simulator.zone_density' pero en Lambda
# están al mismo nivel, necesita 'from zone_density'
echo "    Parcheando imports para entorno Lambda..."
sed -i '' 's/from simulator\.zone_density/from zone_density/' "$BUILD_DIR/fill_model.py"

echo "    Build completado."
echo "    Archivos en $BUILD_DIR: $(ls "$BUILD_DIR" | wc -l | tr -d ' ') items"
echo "    Tamaño total: $(du -sh "$BUILD_DIR" | cut -f1)"
