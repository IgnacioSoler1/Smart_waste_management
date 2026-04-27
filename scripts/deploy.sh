#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# deploy.sh — SmartWaste MVD
#
# Script unificado de despliegue completo:
#   1. terraform apply (infraestructura)
#   2. post-deploy (seed DynamoDB con datos de contenedores)
#
# Diseñado para que un `terraform destroy` seguido de
# `./scripts/deploy.sh` reconstruya todo el entorno.
#
# Uso:
#   ./scripts/deploy.sh                # deploy completo
#   ./scripts/deploy.sh --dry-run      # solo muestra qué haría
#   ./scripts/deploy.sh --skip-terraform  # solo post-deploy
#   ./scripts/deploy.sh --skip-seed-fill  # sin fill levels iniciales
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TERRAFORM_DIR="$PROJECT_DIR/terraform"

DRY_RUN=""
SKIP_TERRAFORM=""
EXTRA_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --dry-run)          DRY_RUN="true"; EXTRA_ARGS+=("--dry-run") ;;
        --skip-terraform)   SKIP_TERRAFORM="true" ;;
        --skip-seed-fill)   EXTRA_ARGS+=("--skip-seed-fill") ;;
        *) echo "Argumento desconocido: $arg"; exit 1 ;;
    esac
done

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  SmartWaste MVD — Deploy Completo"
echo "══════════════════════════════════════════════════════════"
echo ""

# ── Paso 1: Terraform ──────────────────────────────────────
if [[ -z "$SKIP_TERRAFORM" ]]; then
    echo "==> [Paso 1] Terraform Apply"
    echo ""

    cd "$TERRAFORM_DIR"

    if [[ -n "$DRY_RUN" ]]; then
        echo "  [DRY RUN] terraform plan"
        terraform plan -refresh=false
    else
        terraform apply -refresh=false -auto-approve
    fi

    cd "$PROJECT_DIR"
    echo ""
else
    echo "==> [Paso 1] Terraform: OMITIDO (--skip-terraform)"
    echo ""
fi

# ── Paso 2: Post-deploy (seed data) ────────────────────────
echo "==> [Paso 2] Post-Deploy (seed DynamoDB)"
echo ""

"$SCRIPT_DIR/post-deploy.sh" "${EXTRA_ARGS[@]}"

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  Deploy completo."
echo ""
echo "  Próximos pasos:"
echo "    - El sensor-simulator Lambda corre cada 10 min (EventBridge)"
echo "    - El route-optimizer Lambda corre cada 15 min (EventBridge)"
echo "    - Verificar frontend: ver rutas optimizadas en el mapa"
echo "══════════════════════════════════════════════════════════"
