#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# post-deploy.sh — SmartWaste MVD
#
# Script que se ejecuta DESPUÉS de `terraform apply` para
# cargar datos que Terraform no puede manejar (DynamoDB seed).
#
# Uso:
#   ./scripts/post-deploy.sh              # deploy completo
#   ./scripts/post-deploy.sh --dry-run    # solo muestra qué haría
#   ./scripts/post-deploy.sh --skip-seed-fill  # sin fill levels
#
# Requisitos:
#   - terraform apply ya ejecutado (tablas DynamoDB deben existir)
#   - Python 3.11+ con boto3, pyproj
#   - AWS CLI configurado (profile: personal-smart-recycle)
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Configuración ───────────────────────────────────────────
AWS_PROFILE="${AWS_PROFILE:-personal-smart-recycle}"
AWS_REGION="${AWS_REGION:-us-east-1}"
NAME_PREFIX="${NAME_PREFIX:-smartwaste-dev}"
CONTAINERS_TABLE="${NAME_PREFIX}-containers"

DRY_RUN=""
SKIP_SEED_FILL=""

for arg in "$@"; do
    case "$arg" in
        --dry-run)       DRY_RUN="--dry-run" ;;
        --skip-seed-fill) SKIP_SEED_FILL="true" ;;
        *) echo "Argumento desconocido: $arg"; exit 1 ;;
    esac
done

echo "══════════════════════════════════════════════════════════"
echo "  SmartWaste MVD — Post-Deploy"
echo "══════════════════════════════════════════════════════════"
echo "  Proyecto  : $PROJECT_DIR"
echo "  Perfil AWS: $AWS_PROFILE"
echo "  Región    : $AWS_REGION"
echo "  Tabla     : $CONTAINERS_TABLE"
if [[ -n "$DRY_RUN" ]]; then
    echo "  Modo      : DRY RUN (sin escrituras)"
fi
echo ""

# ── Verificar que Terraform haya sido aplicado ──────────────
echo "==> [1/3] Verificando que la tabla DynamoDB existe..."
if ! aws dynamodb describe-table \
    --table-name "$CONTAINERS_TABLE" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'Table.TableStatus' \
    --output text 2>/dev/null; then
    echo ""
    echo "ERROR: La tabla '$CONTAINERS_TABLE' no existe."
    echo "       Ejecuta 'cd terraform && terraform apply' primero."
    exit 1
fi
echo "    Tabla OK."
echo ""

# ── Verificar que existan los datos procesados ──────────────
ENRICHED_JSON="$PROJECT_DIR/data/processed/containers_enriched.json"
if [[ ! -f "$ENRICHED_JSON" ]]; then
    echo "==> [1.5/3] Generando datos procesados (convert + consolidate)..."

    RAW_CSV="$PROJECT_DIR/data/raw/Contenedores_domiciliarios.csv"
    if [[ ! -f "$RAW_CSV" ]]; then
        echo ""
        echo "ERROR: No se encontró el CSV raw en: $RAW_CSV"
        echo "       Descargalo de catalogodatos.gub.uy y colocalo en data/raw/"
        exit 1
    fi

    echo "    Convirtiendo coordenadas UTM → WGS84..."
    python3 "$PROJECT_DIR/data/scripts/convert_coordinates.py" \
        --containers "$RAW_CSV" \
        --output "$PROJECT_DIR/data/processed/"

    echo "    Consolidando datos enriquecidos..."
    python3 "$PROJECT_DIR/data/scripts/consolidate_data.py" \
        --processed-dir "$PROJECT_DIR/data/processed/"

    echo "    Datos procesados generados."
    echo ""
fi

# ── Seed contenedores en DynamoDB ───────────────────────────
echo "==> [2/3] Cargando contenedores en DynamoDB..."

CONTAINER_COUNT=$(python3 -c "import json; print(len(json.load(open('$ENRICHED_JSON'))))")
echo "    Contenedores a cargar: $CONTAINER_COUNT"

python3 "$PROJECT_DIR/data/scripts/seed_dynamodb.py" \
    --input "$ENRICHED_JSON" \
    --table-name "$CONTAINERS_TABLE" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    $DRY_RUN

echo ""

# ── Seed fill levels ────────────────────────────────────────
if [[ -z "$SKIP_SEED_FILL" ]]; then
    echo "==> [3/3] Generando fill levels iniciales..."

    PYTHONPATH="$PROJECT_DIR" python3 "$PROJECT_DIR/data/scripts/seed_fill_levels.py" \
        --table-name "$CONTAINERS_TABLE" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --threshold 40 \
        --hours-range 12 72 \
        $DRY_RUN

    echo ""
else
    echo "==> [3/3] Seed fill levels: OMITIDO (--skip-seed-fill)"
    echo "    El sensor-simulator Lambda generará fill levels en la próxima ejecución (cada 10 min)."
    echo ""
fi

# ── Resumen ─────────────────────────────────────────────────
echo "══════════════════════════════════════════════════════════"
if [[ -n "$DRY_RUN" ]]; then
    echo "  DRY RUN completado. Ejecuta sin --dry-run para aplicar."
else
    echo "  Post-deploy completado."
    echo ""
    echo "  Verificar:"
    echo "    aws dynamodb scan --table-name $CONTAINERS_TABLE --select COUNT \\"
    echo "      --region $AWS_REGION --profile $AWS_PROFILE"
    echo ""
    echo "  El sensor-simulator Lambda se ejecutará automáticamente"
    echo "  cada 10 minutos via EventBridge y actualizará los fill levels."
fi
echo "══════════════════════════════════════════════════════════"
