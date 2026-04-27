#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# cleanup-iot-manual.sh — SmartWaste MVD
#
# Elimina el IoT Thing y certificados que se crearon manualmente
# para el simulador local. Ya no se necesitan porque el simulador
# ahora corre como Lambda usando IAM (boto3 iot-data.publish).
#
# Uso:
#   ./scripts/cleanup-iot-manual.sh              # ejecutar
#   ./scripts/cleanup-iot-manual.sh --dry-run    # solo mostrar
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

AWS_PROFILE="${AWS_PROFILE:-personal-smart-recycle}"
AWS_REGION="${AWS_REGION:-us-east-1}"

DRY_RUN=""
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN="true"

THING_NAME="smartwaste-dev-simulator"
CERT_ARN_FILE="$PROJECT_DIR/simulator/certs/cert_arn.txt"

echo "══════════════════════════════════════════════════════════"
echo "  Cleanup IoT Thing y certificados manuales"
echo "══════════════════════════════════════════════════════════"

# ── Obtener el ARN del certificado ──────────────────────────
if [[ -f "$CERT_ARN_FILE" ]]; then
    CERT_ARN=$(cat "$CERT_ARN_FILE")
    CERT_ID=$(echo "$CERT_ARN" | sed 's|.*/||')
    echo "  Certificado: $CERT_ID"
else
    echo "  No se encontró cert_arn.txt, buscando certificados del Thing..."
    CERT_ARN=$(aws iot list-thing-principals \
        --thing-name "$THING_NAME" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --query 'principals[0]' \
        --output text 2>/dev/null || echo "")
    if [[ -z "$CERT_ARN" || "$CERT_ARN" == "None" ]]; then
        echo "  No se encontraron certificados para el Thing '$THING_NAME'."
        echo "  Nada que limpiar."
        exit 0
    fi
    CERT_ID=$(echo "$CERT_ARN" | sed 's|.*/||')
    echo "  Certificado encontrado: $CERT_ID"
fi

echo "  Thing: $THING_NAME"
echo ""

if [[ -n "$DRY_RUN" ]]; then
    echo "  [DRY RUN] Se ejecutarían:"
    echo "    1. Desactivar certificado $CERT_ID"
    echo "    2. Desattachear policy del certificado"
    echo "    3. Desattachear certificado del Thing"
    echo "    4. Eliminar certificado"
    echo "    5. Eliminar Thing '$THING_NAME'"
    echo "    6. Eliminar archivos locales en simulator/certs/"
    exit 0
fi

echo "  [1/6] Desactivando certificado..."
aws iot update-certificate \
    --certificate-id "$CERT_ID" \
    --new-status INACTIVE \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" 2>/dev/null || echo "    (ya inactivo o no existe)"

echo "  [2/6] Desattacheando policies..."
POLICIES=$(aws iot list-attached-policies \
    --target "$CERT_ARN" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'policies[].policyName' \
    --output text 2>/dev/null || echo "")

for policy in $POLICIES; do
    echo "    Detach policy: $policy"
    aws iot detach-policy \
        --policy-name "$policy" \
        --target "$CERT_ARN" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" 2>/dev/null || true
done

echo "  [3/6] Desattacheando certificado del Thing..."
aws iot detach-thing-principal \
    --thing-name "$THING_NAME" \
    --principal "$CERT_ARN" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" 2>/dev/null || echo "    (ya desattacheado o no existe)"

echo "  [4/6] Eliminando certificado..."
aws iot delete-certificate \
    --certificate-id "$CERT_ID" \
    --force-delete \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" 2>/dev/null || echo "    (ya eliminado)"

echo "  [5/6] Eliminando Thing..."
aws iot delete-thing \
    --thing-name "$THING_NAME" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" 2>/dev/null || echo "    (ya eliminado)"

echo "  [6/6] Eliminando archivos locales de certificados..."
rm -rf "$PROJECT_DIR/simulator/certs/"
echo "    Directorio simulator/certs/ eliminado."

echo ""
echo "  Cleanup completado."
echo "══════════════════════════════════════════════════════════"
