#!/usr/bin/env bash
# generate_device_cert.sh — Generate a device certificate signed by the CA
#
# Usage:
#   ./generate_device_cert.sh <DEVICE_ID> [CA_CERT] [CA_KEY] [OUTPUT_DIR]
#
# Example:
#   ./generate_device_cert.sh smartwaste-dev-101941
#   ./generate_device_cert.sh smartwaste-dev-101941 ca.cert.pem ca.key.pem ./certs/
#
# Output:
#   {output_dir}/{device_id}.cert.pem   — Device certificate (flash to NVS)
#   {output_dir}/{device_id}.key.pem    — Device private key (flash to NVS)

set -euo pipefail

DEVICE_ID="${1:?Usage: $0 <DEVICE_ID> [CA_CERT] [CA_KEY] [OUTPUT_DIR]}"
CA_CERT="${2:-ca.cert.pem}"
CA_KEY="${3:-ca.key.pem}"
OUTPUT_DIR="${4:-.}"

DEVICE_CERT="${OUTPUT_DIR}/${DEVICE_ID}.cert.pem"
DEVICE_KEY="${OUTPUT_DIR}/${DEVICE_ID}.key.pem"
DEVICE_CSR="${OUTPUT_DIR}/${DEVICE_ID}.csr.pem"

echo "=== SmartWaste MVD — Device Certificate Generator ==="
echo "Device ID:  ${DEVICE_ID}"
echo "CA cert:    ${CA_CERT}"
echo "Output dir: ${OUTPUT_DIR}"
echo ""

# Verify CA files exist
if [ ! -f "${CA_CERT}" ]; then
    echo "ERROR: CA certificate not found: ${CA_CERT}"
    echo "Run register_ca.sh first to create the CA."
    exit 1
fi

if [ ! -f "${CA_KEY}" ]; then
    echo "ERROR: CA private key not found: ${CA_KEY}"
    exit 1
fi

mkdir -p "${OUTPUT_DIR}"

# Step 1: Generate device private key (RSA 2048)
echo "1. Generating device private key..."
openssl genrsa -out "${DEVICE_KEY}" 2048 2>/dev/null
chmod 600 "${DEVICE_KEY}"

# Step 2: Generate Certificate Signing Request (CSR)
# The CN (Common Name) becomes the ThingName in JITP
echo "2. Generating CSR with CN=${DEVICE_ID}..."
openssl req -new \
    -key "${DEVICE_KEY}" \
    -out "${DEVICE_CSR}" \
    -subj "/CN=${DEVICE_ID}/O=SmartWaste MVD/C=UY"

# Step 3: Sign the CSR with the CA
echo "3. Signing certificate with CA..."
openssl x509 -req \
    -in "${DEVICE_CSR}" \
    -CA "${CA_CERT}" \
    -CAkey "${CA_KEY}" \
    -CAcreateserial \
    -out "${DEVICE_CERT}" \
    -days 3650 \
    -sha256

# Clean up CSR (not needed after signing)
rm -f "${DEVICE_CSR}"

echo ""
echo "=== Certificate generated successfully ==="
echo "  Certificate: ${DEVICE_CERT}"
echo "  Private key: ${DEVICE_KEY}"
echo ""
echo "=== Next steps ==="
echo "1. Flash the certificate and key to the ESP32 NVS:"
echo ""
echo "   # Create NVS partition binary with the certs:"
echo "   python \$IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py generate \\"
echo "     nvs_data.csv nvs_data.bin 0x10000"
echo ""
echo "   # Flash to NVS partition:"
echo "   esptool.py --port /dev/ttyUSB0 write_flash 0x9000 nvs_data.bin"
echo ""
echo "2. On first connection, AWS IoT Core will automatically:"
echo "   - Create a Thing named '${DEVICE_ID}'"
echo "   - Activate the certificate"
echo "   - Attach the 'smartwaste-dev-sensor-policy'"
echo ""
echo "3. Verify in AWS Console:"
echo "   aws iot describe-thing --thing-name ${DEVICE_ID}"
