#!/usr/bin/env bash
# build-data.sh — SmartWaste MVD
#
# Descarga el mapa de Uruguay y prepara los datos de OSRM.
#
# Uso (primera vez o para actualizar):
#   cd osrm/
#   ./build-data.sh
#
# Tiempo estimado: ~5 minutos (red + CPU)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
PBF="$DATA_DIR/uruguay-latest.osm.pbf"
URL="https://download.geofabrik.de/south-america/uruguay-latest.osm.pbf"

mkdir -p "$DATA_DIR"

# ── Paso 1: Descargar PBF en el host ──────────────────────────────────────────
# La imagen de OSRM es Debian Stretch (EOL) y no tiene curl ni wget instalables.
# macOS incluye curl nativo, así que lo usamos directamente.
echo "==> [1/4] Descargando uruguay-latest.osm.pbf desde Geofabrik..."
echo "    Destino: $PBF"

# -z: timestamping — sólo descarga si el archivo remoto es más nuevo
curl -L --progress-bar \
     -z "$PBF" \
     -o "$PBF" \
     "$URL"

echo "    Descarga completada ($(du -sh "$PBF" | cut -f1))"

# ── Pasos 2-4: Procesamiento OSRM dentro del contenedor ───────────────────────
echo ""
echo "==> [2-4/4] Procesando con OSRM (extract → partition → customize)..."
echo "    Esto puede tardar ~5 minutos en la primera ejecución."
echo ""

docker compose -f "$SCRIPT_DIR/docker-compose.yml" run --rm osrm-prepare

echo ""
echo "==> ¡Listo! Datos preparados en $DATA_DIR"
echo ""
echo "    Para levantar el servidor:"
echo "      docker compose up osrm-server"
echo ""
echo "    Para probar el servidor:"
echo "      python osrm/test_osrm.py"
