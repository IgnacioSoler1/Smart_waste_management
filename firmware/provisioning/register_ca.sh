#!/usr/bin/env bash
# register_ca.sh — Create a CA and register it in AWS IoT Core for JITR
#
# This script:
#   1. Generates a root CA key + self-signed certificate
#   2. Gets a registration code from AWS IoT Core
#   3. Creates a verification certificate signed by the CA
#   4. Registers the CA in IoT Core with auto-registration enabled
#
# The JITR Lambda and IoT Rule are managed by Terraform (see terraform/iot.tf).
# After running this script, set iot_ca_certificate_id in terraform.tfvars
# and run `terraform apply` to create the JITR infrastructure.
#
# Usage:
#   ./register_ca.sh [OUTPUT_DIR] [AWS_PROFILE]
#
# Example:
#   ./register_ca.sh ./ca-keys personal-classify
#
# Prerequisites:
#   - AWS CLI v2 configured with appropriate permissions
#   - openssl

set -euo pipefail

OUTPUT_DIR="${1:-./ca-keys}"
AWS_PROFILE="${2:-personal-classify}"

CA_KEY="${OUTPUT_DIR}/ca.key.pem"
CA_CERT="${OUTPUT_DIR}/ca.cert.pem"
VERIFY_KEY="${OUTPUT_DIR}/verify.key.pem"
VERIFY_CSR="${OUTPUT_DIR}/verify.csr.pem"
VERIFY_CERT="${OUTPUT_DIR}/verify.cert.pem"

echo "=== SmartWaste MVD — CA Registration for JITR ==="
echo "Output dir:    ${OUTPUT_DIR}"
echo "AWS profile:   ${AWS_PROFILE}"
echo ""

mkdir -p "${OUTPUT_DIR}"
chmod 700 "${OUTPUT_DIR}"

# Step 1: Generate CA private key
echo "1. Generating CA private key (RSA 2048)..."
openssl genrsa -out "${CA_KEY}" 2048 2>/dev/null
chmod 600 "${CA_KEY}"

# Step 2: Generate self-signed CA certificate
# AWS IoT Core requires basicConstraints=CA:TRUE to accept this as a CA.
echo "2. Generating self-signed CA certificate (10 years)..."
openssl req -x509 -new -nodes \
    -key "${CA_KEY}" \
    -sha256 -days 3650 \
    -out "${CA_CERT}" \
    -subj "/CN=SmartWaste MVD Root CA/O=SmartWaste MVD/C=UY" \
    -addext "basicConstraints=critical,CA:TRUE"

# Step 3: Get registration code from AWS IoT Core
echo "3. Getting registration code from AWS IoT Core..."
REG_CODE=$(aws iot get-registration-code \
    --profile "${AWS_PROFILE}" \
    --query 'registrationCode' \
    --output text)

echo "   Registration code: ${REG_CODE}"

# Step 4: Generate verification certificate
echo "4. Generating verification certificate..."
openssl genrsa -out "${VERIFY_KEY}" 2048 2>/dev/null

openssl req -new \
    -key "${VERIFY_KEY}" \
    -out "${VERIFY_CSR}" \
    -subj "/CN=${REG_CODE}"

openssl x509 -req \
    -in "${VERIFY_CSR}" \
    -CA "${CA_CERT}" \
    -CAkey "${CA_KEY}" \
    -CAcreateserial \
    -out "${VERIFY_CERT}" \
    -days 365 \
    -sha256

# Step 5: Register CA in AWS IoT Core with auto-registration
echo "5. Registering CA in AWS IoT Core (with auto-registration)..."

CA_ID=$(aws iot register-ca-certificate \
    --ca-certificate "file://${CA_CERT}" \
    --verification-certificate "file://${VERIFY_CERT}" \
    --set-as-active \
    --allow-auto-registration \
    --profile "${AWS_PROFILE}" \
    --query 'certificateId' \
    --output text)

echo "   CA Certificate ID: ${CA_ID}"

# Clean up verification files
rm -f "${VERIFY_KEY}" "${VERIFY_CSR}" "${VERIFY_CERT}" "${CA_CERT%.pem}.srl"

echo ""
echo "=== CA registered successfully ==="
echo "  CA Certificate ID: ${CA_ID}"
echo "  CA Certificate:    ${CA_CERT}"
echo "  CA Private Key:    ${CA_KEY} (KEEP THIS SAFE!)"
echo ""
echo "=== IMPORTANT ==="
echo "  - Keep '${CA_KEY}' secure — it signs all device certificates"
echo "  - Do NOT commit the CA key to git"
echo "  - Back up '${OUTPUT_DIR}' to a secure location"
echo ""
echo "=== Next steps ==="
echo "  1. Add the CA ID to terraform/terraform.tfvars:"
echo "     iot_ca_certificate_id = \"${CA_ID}\""
echo ""
echo "  2. Deploy the JITR infrastructure:"
echo "     cd ../../terraform && terraform apply"
echo ""
echo "  3. Generate device certificates:"
echo "     ./generate_device_cert.sh <DEVICE_ID> ${CA_CERT} ${CA_KEY}"
