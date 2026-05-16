#!/bin/bash
set -ex

# AL2 ECS GPU AMI has Docker + NVIDIA drivers pre-installed.
# Stop ECS agent — we only need Docker.
systemctl stop ecs || true
systemctl disable ecs || true

# Login to ECR using variables injected by Terraform
REGION="${region}"
ACCOUNT="${account_id}"

# Usamos directamente la variable ACCOUNT que nos da Terraform
aws ecr get-login-password --region "$REGION" | \
  docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com" || {
    # Por si acaso la AMI de verdad no tiene el comando aws, instalamos amazon-ecr-credential-helper o usamos el fallback clásico:
    # Amazon Linux 2 incluye una herramienta nativa alternativa si falla la anterior:
    $(amazon-ecr-credential-helper get) || true
  }

# NOTA DE SEGURIDAD: Si el login de arriba falla porque no encuentra 'aws', 
# la forma nativa en Amazon Linux para loguearse a ECR sin el CLI es usando esta línea:
if ! command -v aws &> /dev/null; then
    echo "aws-cli no encontrado, instalando una versión minimalista o usando login alternativo..."
    # Instalar aws-cli rápidamente de los repositorios de Amazon
    yum install -y aws-cli
fi

# Ahora que nos aseguramos de que 'aws' existe o está instalado:
aws ecr get-login-password --region "$REGION" | \
  docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

# Pull cuOpt from ECR
ECR_REPO="${ecr_repo_url}"
docker pull "$ECR_REPO:latest"

# Run cuOpt with GPU
docker run -d \
  --name cuopt \
  --gpus all \
  -p 5000:5000 \
  -e NGC_API_KEY="${cuopt_api_key}" \
  --restart always \
  "$ECR_REPO:latest" \


# Wait for cuOpt to be healthy (up to 10 min)
for i in $(seq 1 60); do
  if curl -sf http://localhost:5000/cuopt/health; then
    echo "cuOpt is healthy"
    break
  fi
  sleep 10
done