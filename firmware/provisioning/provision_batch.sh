#!/usr/bin/env bash
# provision_batch.sh — Generate certificates and NVS binaries for all containers
#
# Reads a CSV of containers (with container_id, circuit_id, latitude, longitude)
# and generates per-device certificates and NVS partition binaries.
#
# Usage:
#   ./provision_batch.sh \
#       --containers ../data/processed/containers_wgs84.csv \
#       --ca-cert ./ca-keys/ca.cert.pem \
#       --ca-key ./ca-keys/ca.key.pem \
#       --height 1200 \
#       --output ./batch_output/
#
# Output:
#   batch_output/
#   ├── device-certs/
#   │   ├── smartwaste-dev-{gid}.cert.pem
#   │   └── smartwaste-dev-{gid}.key.pem
#   ├── nvs_binaries/
#   │   ├── nvs_{gid}.bin
#   │   └── manifest.csv
#   └── nvs_csvs/  (intermediate, can be deleted)

set -euo pipefail

# ── Parse arguments ───────────────────────────────────────

CONTAINERS_CSV=""
CA_CERT=""
CA_KEY=""
CONTAINER_HEIGHT="1200"
OUTPUT_DIR=""

usage() {
    echo "Usage: $0 --containers <csv> --ca-cert <pem> --ca-key <pem> [--height <mm>] --output <dir>"
    echo ""
    echo "Options:"
    echo "  --containers  Path to containers CSV (must have: container_id, circuit_id, latitude, longitude)"
    echo "  --ca-cert     Path to CA certificate (PEM)"
    echo "  --ca-key      Path to CA private key (PEM)"
    echo "  --height      Container height in mm (default: 1200)"
    echo "  --output      Output directory for certs and NVS binaries"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --containers) CONTAINERS_CSV="$2"; shift 2 ;;
        --ca-cert)    CA_CERT="$2"; shift 2 ;;
        --ca-key)     CA_KEY="$2"; shift 2 ;;
        --height)     CONTAINER_HEIGHT="$2"; shift 2 ;;
        --output)     OUTPUT_DIR="$2"; shift 2 ;;
        -h|--help)    usage ;;
        *)            echo "Unknown option: $1"; usage ;;
    esac
done

if [[ -z "$CONTAINERS_CSV" || -z "$CA_CERT" || -z "$CA_KEY" || -z "$OUTPUT_DIR" ]]; then
    echo "ERROR: Missing required arguments."
    usage
fi

if [[ ! -f "$CONTAINERS_CSV" ]]; then
    echo "ERROR: Containers CSV not found: $CONTAINERS_CSV"
    exit 1
fi

if [[ ! -f "$CA_CERT" ]]; then
    echo "ERROR: CA certificate not found: $CA_CERT"
    exit 1
fi

if [[ ! -f "$CA_KEY" ]]; then
    echo "ERROR: CA private key not found: $CA_KEY"
    exit 1
fi

# Check for IDF_PATH (needed for NVS partition generator)
if [[ -z "${IDF_PATH:-}" ]]; then
    echo "ERROR: IDF_PATH is not set. Source the ESP-IDF environment first:"
    echo "  source \"\$HOME/.espressif/tools/activate_idf_v6.0.1.sh\""
    exit 1
fi

NVS_GEN="$IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"
if [[ ! -f "$NVS_GEN" ]]; then
    echo "ERROR: NVS partition generator not found at: $NVS_GEN"
    exit 1
fi

# ── Setup directories ─────────────────────────────────────

CERTS_DIR="$OUTPUT_DIR/device-certs"
NVS_BIN_DIR="$OUTPUT_DIR/nvs_binaries"
NVS_CSV_DIR="$OUTPUT_DIR/nvs_csvs"
MANIFEST="$NVS_BIN_DIR/manifest.csv"

mkdir -p "$CERTS_DIR" "$NVS_BIN_DIR" "$NVS_CSV_DIR"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== SmartWaste MVD — Batch Provisioning ==="
echo "Containers CSV: $CONTAINERS_CSV"
echo "CA certificate: $CA_CERT"
echo "Container height: ${CONTAINER_HEIGHT}mm"
echo "Output: $OUTPUT_DIR"
echo ""

# ── Write manifest header ─────────────────────────────────

echo "container_id,circuit_id,latitude,longitude,cert_path,key_path,nvs_path" > "$MANIFEST"

# ── Detect CSV columns ────────────────────────────────────

HEADER=$(head -1 "$CONTAINERS_CSV")

# Find column indices (0-based)
get_col_index() {
    echo "$HEADER" | tr ',' '\n' | grep -n "^$1$" | cut -d: -f1 | head -1
}

COL_ID=$(get_col_index "container_id")
COL_CIRCUIT=$(get_col_index "circuit_id")
COL_LAT=$(get_col_index "latitude")
COL_LON=$(get_col_index "longitude")

if [[ -z "$COL_ID" || -z "$COL_LAT" || -z "$COL_LON" ]]; then
    echo "ERROR: CSV must have columns: container_id, latitude, longitude"
    echo "Found header: $HEADER"
    exit 1
fi

# circuit_id is optional
COL_CIRCUIT="${COL_CIRCUIT:-0}"

# ── Process each container ────────────────────────────────

TOTAL=$(tail -n +2 "$CONTAINERS_CSV" | wc -l | tr -d ' ')
COUNT=0
ERRORS=0

echo "Processing $TOTAL containers..."
echo ""

tail -n +2 "$CONTAINERS_CSV" | while IFS=',' read -r line; do
    # Parse fields
    GID=$(echo "$line" | cut -d',' -f"$COL_ID" | tr -d '"' | tr -d ' ')
    LAT=$(echo "$line" | cut -d',' -f"$COL_LAT" | tr -d '"' | tr -d ' ')
    LON=$(echo "$line" | cut -d',' -f"$COL_LON" | tr -d '"' | tr -d ' ')

    if [[ "$COL_CIRCUIT" != "0" ]]; then
        CIRCUIT=$(echo "$line" | cut -d',' -f"$COL_CIRCUIT" | tr -d '"' | tr -d ' ')
    else
        CIRCUIT=""
    fi

    COUNT=$((COUNT + 1))
    DEVICE_ID="smartwaste-dev-${GID}"

    # Skip if already generated
    if [[ -f "$NVS_BIN_DIR/nvs_${GID}.bin" ]]; then
        echo "  [$COUNT/$TOTAL] $GID — already exists, skipping"
        continue
    fi

    echo -n "  [$COUNT/$TOTAL] $GID — "

    # Step 1: Generate device certificate
    CERT_FILE="$CERTS_DIR/${DEVICE_ID}.cert.pem"
    KEY_FILE="$CERTS_DIR/${DEVICE_ID}.key.pem"

    if [[ ! -f "$CERT_FILE" ]]; then
        "$SCRIPT_DIR/generate_device_cert.sh" "$DEVICE_ID" "$CA_CERT" "$CA_KEY" "$CERTS_DIR" > /dev/null 2>&1
        if [[ $? -ne 0 ]]; then
            echo "FAILED (cert generation)"
            ERRORS=$((ERRORS + 1))
            continue
        fi
    fi

    # Step 2: Generate NVS CSV
    NVS_CSV="$NVS_CSV_DIR/nvs_${GID}.csv"
    cat > "$NVS_CSV" << EOF
key,type,encoding,value
smartwaste,namespace,,
container_id,data,string,$GID
dev_cert,file,string,$CERT_FILE
dev_key,file,string,$KEY_FILE
EOF

    # Step 3: Generate NVS binary
    NVS_BIN="$NVS_BIN_DIR/nvs_${GID}.bin"
    python "$NVS_GEN" generate "$NVS_CSV" "$NVS_BIN" 0x10000 > /dev/null 2>&1
    if [[ $? -ne 0 ]]; then
        echo "FAILED (nvs generation)"
        ERRORS=$((ERRORS + 1))
        continue
    fi

    # Step 4: Add to manifest
    echo "$GID,$CIRCUIT,$LAT,$LON,$CERT_FILE,$KEY_FILE,$NVS_BIN" >> "$MANIFEST"

    echo "OK"
done

echo ""
echo "=== Batch provisioning complete ==="
echo "  Total: $TOTAL"
echo "  Errors: $ERRORS"
echo "  Certs: $CERTS_DIR/"
echo "  NVS binaries: $NVS_BIN_DIR/"
echo "  Manifest: $MANIFEST"
