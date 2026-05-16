# ─────────────────────────────────────────────────────────
# ECS Fargate — SmartWaste MVD
#
# Despliega el servidor OSRM (routing engine) en ECS Fargate
# dentro de la VPC privada. La Lambda route-optimizer lo
# descubre vía Cloud Map DNS: http://osrm.smartwaste.local:5000
#
# Arquitectura:
#   ECR → ECS Task Definition → ECS Service
#   ECS Service → Cloud Map (osrm.smartwaste.local)
#   Lambda (private subnet) → SG rule → ECS task port 5000
# ─────────────────────────────────────────────────────────

locals {
  cuopt_ngc_tag = "25.02" # Tag de la imagen cuOpt en NGC. Ver tags disponibles en NGC catalog.
}

# ── ECR Repository ─────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "osrm" {
  name                 = "${local.name_prefix}-osrm"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "${local.name_prefix}-osrm" }
}

resource "aws_ecr_lifecycle_policy" "osrm" {
  repository = aws_ecr_repository.osrm.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Mantener las últimas 3 imágenes"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

# ── Build + Push imagen OSRM a ECR ────────────────────────────────────────────
# Reconstruye y sube la imagen cuando cambia el Dockerfile o el timestamp
# del mapa de Uruguay (generado por build-data.sh).

resource "null_resource" "osrm_image_push" {
  triggers = {
    dockerfile_hash = filemd5("${path.module}/../osrm/Dockerfile.ecr")
    data_timestamp  = filemd5("${path.module}/../osrm/data/uruguay-latest.osrm.timestamp")
    ecr_repo        = aws_ecr_repository.osrm.repository_url
  }

  provisioner "local-exec" {
    environment = {
      AWS_PROFILE = "personal-classify"
    }
    command = <<-EOT
      set -e
      REGION="${local.region}"
      ACCOUNT="${local.account_id}"
      REPO="${aws_ecr_repository.osrm.repository_url}"

      echo "==> Login a ECR..."
      aws ecr get-login-password --region "$REGION" | \
        docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

      echo "==> Build imagen OSRM (linux/amd64)..."
      docker build \
        --platform linux/amd64 \
        --file "${path.module}/../osrm/Dockerfile.ecr" \
        --tag "$REPO:latest" \
        "${path.module}/../osrm"

      echo "==> Push a ECR..."
      docker push "$REPO:latest"

      echo "==> ✓ Imagen OSRM publicada: $REPO:latest"
    EOT
  }

  depends_on = [aws_ecr_repository.osrm]
}

# ─────────────────────────────────────────────────────────
# cuOpt — Self-hosted on EC2 GPU (optional)
#
# Activated with var.cuopt_self_hosted = true.
# Lambda calls cuOpt via Cloud Map DNS:
#   http://cuopt.smartwaste.local:5000/cuopt/
#
# EC2 g5.2xlarge: 1x A10G 24GB GPU, 8 vCPU, 32GB RAM.
# Uses AL2 ECS GPU AMI for pre-installed Docker + NVIDIA drivers.
# ─────────────────────────────────────────────────────────

# ── ECR Repository ───────────────────────────────────────

resource "aws_ecr_repository" "cuopt" {
  name                 = "${local.name_prefix}-cuopt"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "${local.name_prefix}-cuopt" }
}

resource "aws_ecr_lifecycle_policy" "cuopt" {
  repository = aws_ecr_repository.cuopt.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Mantener las últimas 3 imágenes"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

# ── Pull + Push imagen cuOpt a ECR ───────────────────────
# Descarga la imagen oficial de NVIDIA NGC y la re-sube a ECR.
# No usa docker build — evita el paso de "export to image" que
# requiere ~6 GB de espacio local con la imagen completa de cuOpt.

resource "null_resource" "cuopt_image_push" {
  count = var.cuopt_self_hosted ? 1 : 0

  triggers = {
    # Usamos el tag que encontraste en GitHub
    cuopt_tag = "latest-cuda12.4-py3.11" # Ajustado a un estándar común, o usa "latest-cuda12.9-py3.13" si prefieres
    ecr_repo  = aws_ecr_repository.cuopt.repository_url
  }

  provisioner "local-exec" {
    environment = {
      AWS_PROFILE = "personal-classify"
    }
    command = <<-EOT
      set -e
      REGION="${local.region}"
      ACCOUNT="${local.account_id}"
      REPO="${aws_ecr_repository.cuopt.repository_url}"
      
      # Usamos EXACTAMENTE la imagen del repo oficial de GitHub
      GITHUB_IMAGE="nvidia/cuopt:26.4.0-cuda13.0-py3.13"

      echo "==> Login a ECR..."
      aws ecr get-login-password --region "$REGION" | \
        docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

      echo "==> Pull imagen cuOpt pública desde GitHub/DockerHub..."
      # Intentamos bajarla directo sin el login restrictivo de NGC
      docker pull --platform linux/amd64 "$GITHUB_IMAGE"

      echo "==> Tag para ECR..."
      docker tag "$GITHUB_IMAGE" "$REPO:latest"

      echo "==> Push a ECR..."
      docker push "$REPO:latest"

      echo "==> Limpieza imagen local..."
      docker rmi "$GITHUB_IMAGE" || true

      echo "==> ✓ Imagen cuOpt publicada: $REPO:latest"
    EOT
  }

  depends_on = [aws_ecr_repository.cuopt]
}

# ── CloudWatch Log Group ─────────────────────────────────

resource "aws_cloudwatch_log_group" "cuopt" {
  count             = var.cuopt_self_hosted ? 1 : 0
  name              = "/ecs/${local.name_prefix}/cuopt"
  retention_in_days = 7
  tags              = { Name = "${local.name_prefix}-cuopt-logs" }
}

# ── Cloud Map — Service Discovery ────────────────────────

resource "aws_service_discovery_service" "cuopt" {
  count = var.cuopt_self_hosted ? 1 : 0
  name  = "cuopt"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.smartwaste.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }

  tags = { Name = "${local.name_prefix}-sd-cuopt" }
}

# ── Security Group ───────────────────────────────────────

resource "aws_security_group" "cuopt" {
  count       = var.cuopt_self_hosted ? 1 : 0
  name        = "${local.name_prefix}-cuopt-sg"
  description = "cuOpt Fargate - accepts port 8080 from route-optimizer Lambda"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-cuopt-sg" }
}

# Regla cruzada: Lambda → cuOpt en puerto 5000
resource "aws_security_group_rule" "lambda_to_cuopt" {
  count                    = var.cuopt_self_hosted ? 1 : 0
  type                     = "egress"
  description              = "cuOpt HTTP API"
  from_port                = 5000
  to_port                  = 5000
  protocol                 = "tcp"
  security_group_id        = aws_security_group.route_optimizer_lambda.id
  source_security_group_id = aws_security_group.cuopt[0].id
}

resource "aws_security_group_rule" "cuopt_from_lambda" {
  count                    = var.cuopt_self_hosted ? 1 : 0
  type                     = "ingress"
  description              = "cuOpt HTTP desde Lambda route-optimizer"
  from_port                = 5000
  to_port                  = 5000
  protocol                 = "tcp"
  security_group_id        = aws_security_group.cuopt[0].id
  source_security_group_id = aws_security_group.route_optimizer_lambda.id
}

resource "aws_security_group_rule" "cuopt_endpoint_https" {
  count             = var.cuopt_self_hosted ? 1 : 0
  type              = "ingress"
  description       = "Permitir HTTPS interno para VPC Endpoints"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  security_group_id = aws_security_group.cuopt[0].id
  cidr_blocks       = [aws_vpc.main.cidr_block]
}

# ── EC2 GPU Instance — cuOpt Server ──────────────────────
#
# g5.2xlarge: 1x A10G 24GB GPU, 8 vCPU, 32GB RAM.
# Uses Amazon Linux 2 ECS GPU AMI (Docker + NVIDIA drivers pre-installed).
# We disable the ECS agent — only Docker is needed.

data "aws_ami" "gpu_al2" {
  count       = var.cuopt_self_hosted ? 1 : 0
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["amzn2-ami-ecs-gpu-hvm-*-x86_64-ebs"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# ── IAM Role for EC2 ─────────────────────────────────────

resource "aws_iam_role" "cuopt_ec2" {
  count = var.cuopt_self_hosted ? 1 : 0
  name  = "${local.name_prefix}-cuopt-ec2"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name_prefix}-cuopt-ec2-role" }
}

resource "aws_iam_role_policy" "cuopt_ec2_ecr" {
  count = var.cuopt_self_hosted ? 1 : 0
  name  = "ecr-pull"
  role  = aws_iam_role.cuopt_ec2[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "ECRPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = aws_ecr_repository.cuopt.arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "cuopt_ec2_logs" {
  count = var.cuopt_self_hosted ? 1 : 0
  name  = "cloudwatch-logs"
  role  = aws_iam_role.cuopt_ec2[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
      ]
      Resource = "${aws_cloudwatch_log_group.cuopt[0].arn}:*"
    }]
  })
}

# SSM Session Manager for debugging
resource "aws_iam_role_policy_attachment" "cuopt_ec2_ssm" {
  count      = var.cuopt_self_hosted ? 1 : 0
  role       = aws_iam_role.cuopt_ec2[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "cuopt" {
  count = var.cuopt_self_hosted ? 1 : 0
  name  = "${local.name_prefix}-cuopt-ec2"
  role  = aws_iam_role.cuopt_ec2[0].name
}

# ── EC2 Instance ─────────────────────────────────────────

resource "aws_instance" "cuopt" {
  count                  = var.cuopt_self_hosted ? 1 : 0
  ami                    = data.aws_ami.gpu_al2[0].id
  instance_type          = "g5.2xlarge"
  subnet_id              = aws_subnet.private[0].id
  vpc_security_group_ids = [aws_security_group.cuopt[0].id]
  iam_instance_profile   = aws_iam_instance_profile.cuopt[0].name

  root_block_device {
    volume_size = 100
    volume_type = "gp3"
  }

  user_data = base64encode(templatefile("${path.module}/templates/cuopt-userdata.sh", {
    ecr_repo_url = aws_ecr_repository.cuopt.repository_url
    region       = local.region
    account_id   = local.account_id
    cuopt_api_key = var.cuopt_api_key
  }))

  tags = { Name = "${local.name_prefix}-cuopt-gpu" }

  depends_on = [null_resource.cuopt_image_push]
}

# ── Cloud Map Registration ───────────────────────────────
# Register EC2's private IP so Lambda discovers cuOpt via DNS:
#   cuopt.smartwaste.local → EC2 private IP

resource "aws_service_discovery_instance" "cuopt" {
  count       = var.cuopt_self_hosted ? 1 : 0
  instance_id = aws_instance.cuopt[0].id
  service_id  = aws_service_discovery_service.cuopt[0].id

  attributes = {
    AWS_INSTANCE_IPV4 = aws_instance.cuopt[0].private_ip
  }
}

# Endpoint para el servicio central de SSM
resource "aws_vpc_endpoint" "ssm" {
  count               = var.cuopt_self_hosted ? 1 : 0
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${local.region}.ssm"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.cuopt[0].id]
  private_dns_enabled = true
}

# Endpoint para los mensajes de SSM (necesario para Session Manager)
resource "aws_vpc_endpoint" "ssmmessages" {
  count               = var.cuopt_self_hosted ? 1 : 0
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${local.region}.ssmmessages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.cuopt[0].id]
  private_dns_enabled = true
}

# Endpoint para interactuar con la EC2
resource "aws_vpc_endpoint" "ec2messages" {
  count               = var.cuopt_self_hosted ? 1 : 0
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${local.region}.ec2messages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.cuopt[0].id]
  private_dns_enabled = true
}

# ── Security Groups ────────────────────────────────────────────────────────────
# Se usan recursos separados para las reglas que se referencian entre sí
# (sg-osrm ↔ sg-route-optimizer) para evitar dependencias circulares.

resource "aws_security_group" "osrm" {
  name        = "${local.name_prefix}-osrm-sg"
  description = "OSRM Fargate - accepts port 5000 from route-optimizer Lambda"
  vpc_id      = aws_vpc.main.id

  # Egress irrestricto: ECR pull via NAT, CloudWatch Logs, etc.
  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-osrm-sg" }
}

resource "aws_security_group" "route_optimizer_lambda" {
  name        = "${local.name_prefix}-route-optimizer-sg"
  description = "Lambda route-optimizer - egress to OSRM:5000 and AWS services via NAT"
  vpc_id      = aws_vpc.main.id

  # No inline egress rules — all rules are managed via aws_security_group_rule
  # to avoid Terraform drift when mixing inline and external rules.

  tags = { Name = "${local.name_prefix}-route-optimizer-sg" }
}

# Egress HTTPS: DynamoDB (via Gateway endpoint), AWS APIs, NVIDIA cuOpt
resource "aws_security_group_rule" "lambda_https" {
  type              = "egress"
  description       = "HTTPS a servicios AWS y externos"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  security_group_id = aws_security_group.route_optimizer_lambda.id
  cidr_blocks       = ["0.0.0.0/0"]
}

# Regla cruzada: Lambda → OSRM en puerto 5000
resource "aws_security_group_rule" "lambda_to_osrm" {
  type                     = "egress"
  description              = "OSRM HTTP API"
  from_port                = 5000
  to_port                  = 5000
  protocol                 = "tcp"
  security_group_id        = aws_security_group.route_optimizer_lambda.id
  source_security_group_id = aws_security_group.osrm.id
}

resource "aws_security_group_rule" "osrm_from_lambda" {
  type                     = "ingress"
  description              = "OSRM HTTP desde Lambda route-optimizer"
  from_port                = 5000
  to_port                  = 5000
  protocol                 = "tcp"
  security_group_id        = aws_security_group.osrm.id
  source_security_group_id = aws_security_group.route_optimizer_lambda.id
}

# ── IAM: ECS Task Execution Role ──────────────────────────────────────────────

resource "aws_iam_role" "ecs_task_execution" {
  name = "${local.name_prefix}-ecs-task-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ── CloudWatch Log Group para OSRM ────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "osrm" {
  name              = "/ecs/${local.name_prefix}/osrm"
  retention_in_days = 7
  tags              = { Name = "${local.name_prefix}-osrm-logs" }
}

# ── Cloud Map — Service Discovery ─────────────────────────────────────────────
# La Lambda accede a OSRM vía DNS: http://osrm.smartwaste.local:5000
# Cloud Map registra la IP privada del task Fargate cuando arranca.

resource "aws_service_discovery_private_dns_namespace" "smartwaste" {
  name        = "smartwaste.local"
  description = "SmartWaste internal service discovery"
  vpc         = aws_vpc.main.id
  tags        = { Name = "${local.name_prefix}-sd-namespace" }
}

resource "aws_service_discovery_service" "osrm" {
  name = "osrm"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.smartwaste.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }

  tags = { Name = "${local.name_prefix}-sd-osrm" }
}

# ── ECS Cluster ───────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = { Name = "${local.name_prefix}-cluster" }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE"]
}

# ── ECS Task Definition: osrm-server ──────────────────────────────────────────

resource "aws_ecs_task_definition" "osrm" {
  family                   = "${local.name_prefix}-osrm"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "2048" # 2 vCPU — headroom para Table API con muchos orígenes
  memory                   = "4096" # 4 GB — OSRM MLD Uruguay ~500 MB real + margen de seguridad
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([{
    name      = "osrm"
    image     = "${aws_ecr_repository.osrm.repository_url}:latest"
    essential = true

    portMappings = [{
      containerPort = 5000
      protocol      = "tcp"
    }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.osrm.name
        "awslogs-region"        = local.region
        "awslogs-stream-prefix" = "osrm"
      }
    }

    # OSRM tarda ~30-60 s en cargar el mapa al arrancar.
    # Chequeamos que el proceso osrm-routed esté vivo y escuchando en :5000.
    healthCheck = {
      command     = ["CMD-SHELL", "curl -sf 'http://127.0.0.1:5000/nearest/v1/driving/-56.1645,-34.9011' -o /dev/null || exit 1"]
      interval    = 30
      timeout     = 10
      retries     = 3
      startPeriod = 90
    }
  }])

  depends_on = [null_resource.osrm_image_push]
  tags       = { Name = "${local.name_prefix}-osrm-task" }
}

# ── ECS Service: osrm-server ───────────────────────────────────────────────────

resource "aws_ecs_service" "osrm" {
  name            = "${local.name_prefix}-osrm"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.osrm.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.osrm.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.osrm.arn
  }

  depends_on = [
    null_resource.osrm_image_push,
    aws_ecs_cluster_capacity_providers.main,
  ]

  tags = { Name = "${local.name_prefix}-osrm-service" }
}
