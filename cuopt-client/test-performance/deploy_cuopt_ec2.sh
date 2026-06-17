#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# deploy_cuopt_ec2.sh — SmartWaste MVD Performance Tests
#
# Lanza/destruye una instancia EC2 g5.2xlarge con cuOpt Docker
# para tests de performance. Independiente de Terraform.
#
# Uso:
#   ./deploy_cuopt_ec2.sh launch           # Lanza instancia, espera health check
#   ./deploy_cuopt_ec2.sh status           # Verifica salud de cuOpt
#   ./deploy_cuopt_ec2.sh run-tests        # Launch + ejecuta benchmarks
#   ./deploy_cuopt_ec2.sh teardown         # Termina instancia y limpia
#   ./deploy_cuopt_ec2.sh run-and-teardown # Todo junto (recomendado)
#
# Variables de entorno opcionales:
#   AWS_PROFILE          - Perfil AWS (default: personal-classify)
#   AWS_REGION           - Región (default: us-east-1)
#   CUOPT_INSTANCE_TYPE  - Tipo EC2 (default: g5.2xlarge)
#   CUOPT_KEY_PAIR       - Key pair SSH (opcional, para debugging)
# ─────────────────────────────────────────────────────────────

AWS_PROFILE="${AWS_PROFILE:-personal-classify}"
AWS_REGION="${AWS_REGION:-us-east-1}"
INSTANCE_TYPE="${CUOPT_INSTANCE_TYPE:-g5.2xlarge}"
# Imagen cuOpt de DockerHub (pública, sin NGC login, sin licencia runtime)
DOCKER_IMAGE="nvidia/cuopt:25.12.0-cuda12.9-py3.13"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/results/.ec2_state.json"
RESULTS_DIR="$SCRIPT_DIR/results"

TAG_KEY="Project"
TAG_VALUE="smartwaste-perftest"
SG_NAME="smartwaste-perftest-sg-$(date +%s)"

# Colores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

aws_cmd() {
    aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"
}

check_dependencies() {
    for cmd in aws jq curl python3; do
        if ! command -v "$cmd" &>/dev/null; then
            log_error "Comando requerido no encontrado: $cmd"
            exit 1
        fi
    done
}

get_my_ip() {
    curl -sf https://checkip.amazonaws.com || curl -sf https://ifconfig.me
}

get_gpu_ami() {
    # Amazon Linux 2 ECS GPU AMI (misma que usa Terraform)
    aws_cmd ec2 describe-images \
        --owners amazon \
        --filters "Name=name,Values=amzn2-ami-ecs-gpu-hvm-*-x86_64-ebs" \
                  "Name=state,Values=available" \
        --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
        --output text
}

create_userdata() {
    cat <<'USERDATA'
#!/bin/bash
set -ex

# Detener ECS agent (no lo necesitamos)
systemctl stop ecs || true
systemctl disable ecs || true

# Docker ya viene instalado en la AMI ECS GPU
# Imagen pública de DockerHub (no requiere NGC login ni licencia)
DOCKER_IMAGE="nvidia/cuopt:25.12.0-cuda12.9-py3.13"
echo "Pulling $DOCKER_IMAGE..."
docker pull "$DOCKER_IMAGE"

# Ejecutar cuOpt con GPU
docker run -d \
    --name cuopt \
    --gpus all \
    -p 5000:5000 \
    --restart always \
    "$DOCKER_IMAGE"

echo "cuOpt container started, waiting for health check..."

# Esperar health check (hasta 10 minutos)
for i in $(seq 1 40); do
    if curl -sf http://localhost:5000/cuopt/health >/dev/null 2>&1; then
        echo "cuOpt is healthy after ~$((i * 15)) seconds"
        exit 0
    fi
    echo "Attempt $i/40 - waiting 15s..."
    sleep 15
done

echo "WARNING: cuOpt health check did not pass within 10 minutes"
USERDATA
}

get_default_vpc() {
    aws_cmd ec2 describe-vpcs \
        --filters "Name=isDefault,Values=true" \
        --query 'Vpcs[0].VpcId' \
        --output text
}

get_default_subnets() {
    # Retorna TODAS las subnets default del VPC, una por línea
    local vpc_id="$1"
    aws_cmd ec2 describe-subnets \
        --filters "Name=vpc-id,Values=$vpc_id" \
                  "Name=default-for-az,Values=true" \
        --query 'Subnets[*].SubnetId' \
        --output text | tr '\t' '\n'
}

launch() {
    check_dependencies
    mkdir -p "$RESULTS_DIR"

    if [[ -f "$STATE_FILE" ]]; then
        log_warn "Ya existe una instancia en $STATE_FILE"
        log_warn "Usa 'teardown' primero o elimina el archivo manualmente."
        exit 1
    fi

    log_info "Buscando AMI GPU..."
    AMI_ID=$(get_gpu_ami)
    log_info "AMI: $AMI_ID"

    log_info "Obteniendo IP pública..."
    MY_IP=$(get_my_ip)
    log_info "Tu IP: $MY_IP"

    # Usar VPC default
    VPC_ID=$(get_default_vpc)
    log_info "VPC: $VPC_ID"

    # Obtener todas las subnets disponibles
    SUBNETS=()
    while IFS= read -r subnet; do
        [[ -n "$subnet" ]] && SUBNETS+=("$subnet")
    done < <(get_default_subnets "$VPC_ID")
    log_info "Subnets disponibles: ${SUBNETS[*]}"

    # Crear Security Group
    log_info "Creando Security Group..."
    SG_ID=$(aws_cmd ec2 create-security-group \
        --group-name "$SG_NAME" \
        --description "cuOpt perf test - temporary" \
        --vpc-id "$VPC_ID" \
        --query 'GroupId' \
        --output text)

    aws_cmd ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" \
        --protocol tcp --port 5000 \
        --cidr "${MY_IP}/32"

    aws_cmd ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" \
        --protocol tcp --port 22 \
        --cidr "${MY_IP}/32"

    log_info "Security Group: $SG_ID (puertos 22, 5000 desde $MY_IP)"

    # Crear user-data
    USERDATA_FILE=$(mktemp)
    create_userdata > "$USERDATA_FILE"

    # Preparar args base para el launch
    BASE_LAUNCH_ARGS=(
        ec2 run-instances
        --image-id "$AMI_ID"
        --instance-type "$INSTANCE_TYPE"
        --security-group-ids "$SG_ID"
        --associate-public-ip-address
        --user-data "file://$USERDATA_FILE"
        --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=100,VolumeType=gp3}"
        --tag-specifications "ResourceType=instance,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE},{Key=Name,Value=cuopt-perftest}]"
        --query 'Instances[0].InstanceId'
        --output text
    )

    # Agregar key pair si está definido
    if [[ -n "${CUOPT_KEY_PAIR:-}" ]]; then
        BASE_LAUNCH_ARGS+=(--key-name "$CUOPT_KEY_PAIR")
    fi

    # Intentar lanzar en cada subnet hasta que una funcione
    INSTANCE_ID=""
    for SUBNET_ID in "${SUBNETS[@]}"; do
        AZ=$(aws_cmd ec2 describe-subnets \
            --subnet-ids "$SUBNET_ID" \
            --query 'Subnets[0].AvailabilityZone' \
            --output text)
        log_info "Intentando lanzar en $AZ (subnet $SUBNET_ID)..."

        INSTANCE_ID=$(aws_cmd "${BASE_LAUNCH_ARGS[@]}" --subnet-id "$SUBNET_ID" 2>&1) && break
        log_warn "No hay capacidad en $AZ, probando siguiente..."
        INSTANCE_ID=""
    done

    rm -f "$USERDATA_FILE"

    if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == *"error"* || "$INSTANCE_ID" == *"Error"* ]]; then
        log_error "No se pudo lanzar la instancia en ninguna AZ."
        log_error "Limpiando Security Group..."
        aws_cmd ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null || true
        exit 1
    fi

    log_info "Instancia: $INSTANCE_ID"
    log_info "Esperando que la instancia esté running..."

    aws_cmd ec2 wait instance-running --instance-ids "$INSTANCE_ID"

    # Obtener IP pública
    PUBLIC_IP=$(aws_cmd ec2 describe-instances \
        --instance-ids "$INSTANCE_ID" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' \
        --output text)

    log_info "IP pública: $PUBLIC_IP"

    # Guardar estado
    cat > "$STATE_FILE" <<EOF
{
    "instance_id": "$INSTANCE_ID",
    "public_ip": "$PUBLIC_IP",
    "sg_id": "$SG_ID",
    "sg_name": "$SG_NAME",
    "ami_id": "$AMI_ID",
    "instance_type": "$INSTANCE_TYPE",
    "launched_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

    # Esperar health check de cuOpt
    log_info "Esperando que cuOpt esté healthy (puede tardar ~5-10 min)..."
    log_info "URL: http://$PUBLIC_IP:5000/cuopt/health"

    for i in $(seq 1 40); do
        if curl -sf "http://$PUBLIC_IP:5000/cuopt/health" >/dev/null 2>&1; then
            log_info "cuOpt HEALTHY (intento $i, ~$((i * 15))s)"
            echo ""
            log_info "====================================="
            log_info "cuOpt listo en http://$PUBLIC_IP:5000"
            log_info "====================================="
            echo ""
            log_info "Costo: ~\$1.21/hora. Recuerda hacer teardown cuando termines."
            return 0
        fi
        echo -ne "\r  Intento $i/40 - esperando 15s..."
        sleep 15
    done

    log_error "cuOpt no respondió después de 10 minutos."
    log_error "Verifica con: ssh ec2-user@$PUBLIC_IP 'sudo docker logs cuopt'"
    return 1
}

teardown() {
    check_dependencies

    if [[ ! -f "$STATE_FILE" ]]; then
        log_warn "No hay instancia activa (no existe $STATE_FILE)"

        # Buscar instancias huérfanas por tag
        log_info "Buscando instancias con tag $TAG_KEY=$TAG_VALUE..."
        ORPHANS=$(aws_cmd ec2 describe-instances \
            --filters "Name=tag:$TAG_KEY,Values=$TAG_VALUE" \
                      "Name=instance-state-name,Values=running,pending,stopping,stopped" \
            --query 'Reservations[].Instances[].InstanceId' \
            --output text)

        if [[ -n "$ORPHANS" && "$ORPHANS" != "None" ]]; then
            log_warn "Instancias huérfanas encontradas: $ORPHANS"
            read -rp "¿Terminar estas instancias? [y/N]: " confirm
            if [[ "$confirm" =~ ^[yYsS]$ ]]; then
                aws_cmd ec2 terminate-instances --instance-ids $ORPHANS
                log_info "Instancias terminadas."
            fi
        fi
        return 0
    fi

    INSTANCE_ID=$(jq -r .instance_id "$STATE_FILE")
    SG_ID=$(jq -r .sg_id "$STATE_FILE")

    log_info "Terminando instancia $INSTANCE_ID..."
    aws_cmd ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null

    log_info "Esperando que la instancia se termine..."
    aws_cmd ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"

    log_info "Eliminando Security Group $SG_ID..."
    # Retry porque puede tardar un poco en liberar ENIs
    for i in $(seq 1 5); do
        if aws_cmd ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null; then
            break
        fi
        sleep 5
    done

    rm -f "$STATE_FILE"
    log_info "Cleanup completado."
}

status() {
    if [[ ! -f "$STATE_FILE" ]]; then
        log_warn "No hay instancia activa."
        return 1
    fi

    PUBLIC_IP=$(jq -r .public_ip "$STATE_FILE")
    INSTANCE_ID=$(jq -r .instance_id "$STATE_FILE")

    echo "Instance: $INSTANCE_ID"
    echo "IP:       $PUBLIC_IP"

    if curl -sf "http://$PUBLIC_IP:5000/cuopt/health" >/dev/null 2>&1; then
        log_info "cuOpt: HEALTHY"
    else
        log_warn "cuOpt: UNHEALTHY o no alcanzable"
    fi

    # Mostrar estado EC2
    STATE=$(aws_cmd ec2 describe-instances \
        --instance-ids "$INSTANCE_ID" \
        --query 'Reservations[0].Instances[0].State.Name' \
        --output text 2>/dev/null || echo "unknown")
    echo "EC2 state: $STATE"

    LAUNCHED=$(jq -r .launched_at "$STATE_FILE")
    echo "Lanzada:  $LAUNCHED"
}

run_tests() {
    # Si no hay instancia, lanzar
    if [[ ! -f "$STATE_FILE" ]]; then
        launch
    fi

    PUBLIC_IP=$(jq -r .public_ip "$STATE_FILE")

    log_info "Ejecutando benchmarks contra http://$PUBLIC_IP:5000..."

    cd "$SCRIPT_DIR"
    python3 run_benchmarks.py \
        --mode self_hosted \
        --server "http://$PUBLIC_IP:5000" \
        "$@"
}

run_and_teardown() {
    launch
    run_tests "$@" || true
    teardown
}

# ── Dispatch ─────────────────────────────────────────────────

case "${1:-help}" in
    launch)
        launch
        ;;
    teardown)
        teardown
        ;;
    status)
        status
        ;;
    run-tests)
        shift
        run_tests "$@"
        ;;
    run-and-teardown)
        shift
        run_and_teardown "$@"
        ;;
    help|--help|-h)
        echo "Uso: $0 {launch|teardown|status|run-tests|run-and-teardown}"
        echo ""
        echo "Comandos:"
        echo "  launch           Lanza EC2 g5.2xlarge con cuOpt, espera health check"
        echo "  status           Verifica estado de la instancia y salud de cuOpt"
        echo "  run-tests        Launch (si necesario) + ejecuta benchmarks"
        echo "  run-and-teardown Launch + benchmarks + cleanup (recomendado)"
        echo "  teardown         Termina instancia y elimina Security Group"
        echo ""
        echo "Costo estimado: ~\$1.21/hora (g5.2xlarge on-demand, us-east-1)"
        echo ""
        echo "Ejemplo:"
        echo "  $0 run-and-teardown --quick"
        echo "  $0 launch"
        echo "  $0 run-tests --sizes 10,50,100 --vehicles 2,4"
        echo "  $0 teardown"
        ;;
    *)
        log_error "Comando desconocido: $1"
        echo "Uso: $0 {launch|teardown|status|run-tests|run-and-teardown}"
        exit 1
        ;;
esac
