#!/usr/bin/env bash
# flash_device.sh — Flash firmware + NVS to a single ESP32
#
# Used in production line to flash a pre-built firmware and the device-specific
# NVS partition containing certificates and container_id.
#
# Usage:
#   ./flash_device.sh --gid 101941 --port /dev/cu.usbserial-0001
#   ./flash_device.sh --gid 101941 --port /dev/cu.usbserial-0001 --nvs-dir ./batch_output/nvs_binaries
#   ./flash_device.sh --gid 101941 --port /dev/cu.usbserial-0001 --firmware ../build/smartwaste.bin
#
# Prerequisites:
#   - ESP-IDF environment sourced (for esptool.py)
#   - Firmware already built (idf.py build)
#   - NVS binary already generated (via provision_batch.sh)

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────

GID=""
PORT=""
NVS_DIR="./batch_output/nvs_binaries"
FIRMWARE_DIR="../build"
NVS_OFFSET="0x9000"
APP_OFFSET="0x10000"
BAUD="460800"

usage() {
    echo "Usage: $0 --gid <container_id> --port <serial_port> [options]"
    echo ""
    echo "Required:"
    echo "  --gid       Container ID (gid from Intendencia data)"
    echo "  --port      Serial port (e.g., /dev/cu.usbserial-0001)"
    echo ""
    echo "Optional:"
    echo "  --nvs-dir   Directory containing NVS binaries (default: ./batch_output/nvs_binaries)"
    echo "  --firmware  Directory containing firmware build output (default: ../build)"
    echo "  --baud      Flash baud rate (default: 460800)"
    exit 1
}

# ── Parse arguments ───────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gid)       GID="$2"; shift 2 ;;
        --port)      PORT="$2"; shift 2 ;;
        --nvs-dir)   NVS_DIR="$2"; shift 2 ;;
        --firmware)  FIRMWARE_DIR="$2"; shift 2 ;;
        --baud)      BAUD="$2"; shift 2 ;;
        -h|--help)   usage ;;
        *)           echo "Unknown option: $1"; usage ;;
    esac
done

if [[ -z "$GID" || -z "$PORT" ]]; then
    echo "ERROR: --gid and --port are required."
    usage
fi

# ── Validate files ────────────────────────────────────────

NVS_BIN="$NVS_DIR/nvs_${GID}.bin"
if [[ ! -f "$NVS_BIN" ]]; then
    echo "ERROR: NVS binary not found: $NVS_BIN"
    echo "Run provision_batch.sh first to generate NVS binaries."
    exit 1
fi

# Find firmware binary (try common names)
FIRMWARE_BIN=""
for candidate in "$FIRMWARE_DIR/smartwaste-sensor.bin" "$FIRMWARE_DIR/firmware.bin" "$FIRMWARE_DIR"/*.bin; do
    if [[ -f "$candidate" ]]; then
        FIRMWARE_BIN="$candidate"
        break
    fi
done

if [[ -z "$FIRMWARE_BIN" ]]; then
    echo "ERROR: Firmware binary not found in: $FIRMWARE_DIR"
    echo "Build the firmware first: cd firmware && idf.py build"
    exit 1
fi

# Check serial port exists
if [[ ! -e "$PORT" ]]; then
    echo "ERROR: Serial port not found: $PORT"
    echo "Available ports:"
    ls /dev/cu.usb* 2>/dev/null || ls /dev/ttyUSB* 2>/dev/null || echo "  (none found)"
    exit 1
fi

# ── Flash ─────────────────────────────────────────────────

echo "=== SmartWaste MVD — Flash Device ==="
echo "  Container: $GID"
echo "  Port: $PORT"
echo "  Firmware: $FIRMWARE_BIN"
echo "  NVS: $NVS_BIN"
echo "  Baud: $BAUD"
echo ""

echo "Flashing firmware + NVS partition..."
esptool.py \
    --port "$PORT" \
    --baud "$BAUD" \
    --chip esp32 \
    write_flash \
    "$APP_OFFSET" "$FIRMWARE_BIN" \
    "$NVS_OFFSET" "$NVS_BIN"

echo ""
echo "=== Flash complete ==="
echo "Device $GID is ready. On first boot it will:"
echo "  1. Read container_id and certificates from NVS"
echo "  2. Connect to the network"
echo "  3. Publish to AWS IoT Core"
echo "  4. JITR will auto-create Thing 'smartwaste-dev-$GID'"
