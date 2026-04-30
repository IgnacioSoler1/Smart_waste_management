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
    aws_nat_gateway.main,
  ]

  tags = { Name = "${local.name_prefix}-osrm-service" }
}
