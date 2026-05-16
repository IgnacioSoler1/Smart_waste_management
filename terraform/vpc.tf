# ─────────────────────────────────────────────────────────
# VPC — SmartWaste MVD
#
# Red privada para:
#   - Lambda route-optimizer (subnets privadas)
#   - ECS Fargate OSRM      (subnets privadas)
#
# Una sola NAT Gateway (us-east-1a) para dev — ahorra ~$32/mes
# respecto a una NAT por AZ. Migrar a una por AZ en prod.
# ─────────────────────────────────────────────────────────

locals {
  azs        = ["us-east-1a", "us-east-1b"]
  pub_cidrs  = ["10.0.0.0/24", "10.0.1.0/24"]
  priv_cidrs = ["10.0.10.0/24", "10.0.11.0/24"]
}

# ── VPC ────────────────────────────────────────────────────────────────────────

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "${local.name_prefix}-vpc" }
}

# ── Subnets ────────────────────────────────────────────────────────────────────

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.pub_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name_prefix}-public-${count.index + 1}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.priv_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.name_prefix}-private-${count.index + 1}" }
}

# ── Internet Gateway ───────────────────────────────────────────────────────────

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name_prefix}-igw" }
}

# ── NAT Gateway ────────────────────────────────────────────────────────────────
# Una sola NAT en us-east-1a para dev.
# Las subnets privadas en us-east-1b también la usan (sin HA entre AZs).

resource "aws_eip" "nat" {
  count      = var.cuopt_self_hosted ? 0 : 1
  domain     = "vpc"
  depends_on = [aws_internet_gateway.main]
  tags       = { Name = "${local.name_prefix}-nat-eip" }
}

resource "aws_nat_gateway" "main" {
  count         = var.cuopt_self_hosted ? 0 : 1
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]
  tags          = { Name = "${local.name_prefix}-nat" }
}

# ── Route Tables ───────────────────────────────────────────────────────────────

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name_prefix}-rt-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name_prefix}-rt-private" }
}

# Ruta a Internet vía NAT — solo cuando cuOpt no es self-hosted.
# Cuando cuopt_self_hosted=true, la Lambda no necesita internet: DynamoDB y S3
# usan Gateway Endpoints, Firehose usa Interface Endpoint, OSRM y cuOpt son VPC-internos.
resource "aws_route" "private_nat" {
  count                  = var.cuopt_self_hosted ? 0 : 1
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[0].id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ── VPC Gateway Endpoints (gratuitos) ─────────────────────────────────────────
# Permite que Lambda/ECS en subnets privadas accedan a DynamoDB y S3
# sin salir por NAT (menor latencia y sin costo de transferencia).

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${local.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${local.name_prefix}-endpoint-dynamodb" }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${local.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.public.id]
  tags              = { Name = "${local.name_prefix}-endpoint-s3" }
}


# ── VPC Interface Endpoint — Kinesis Firehose (~$7.30/mes en dev) ──────────────
# Los Gateway Endpoints solo existen para S3 y DynamoDB.
# Kinesis Firehose necesita un Interface Endpoint para que la Lambda en subnets
# privadas pueda llamar a firehose.put_record() sin salir por NAT Gateway.
# Sin este endpoint, el tráfico usaría rutas de IP pública (aunque físicamente
# quedaría en la red de AWS). Con él, el tráfico usa IPs privadas de la VPC.

# Security group para todos los Interface Endpoints del proyecto.
# Solo permite HTTPS desde dentro de la VPC.
resource "aws_security_group" "vpc_endpoints" {
  name        = "${local.name_prefix}-vpc-endpoints"
  description = "Permite HTTPS entrante desde la VPC hacia Interface Endpoints de AWS"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS desde la VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  tags = { Name = "${local.name_prefix}-vpc-endpoints" }
}

resource "aws_vpc_endpoint" "kinesis_firehose" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${local.region}.kinesis-firehose"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  # Con private_dns_enabled=true, boto3 resuelve el hostname estándar de Firehose
  # (firehose.us-east-1.amazonaws.com) directamente a la IP privada del endpoint.
  # No hay que cambiar ninguna URL en el código.
  private_dns_enabled = true

  tags = { Name = "${local.name_prefix}-endpoint-firehose" }
}

# ── VPC Interface Endpoints — ECR + CloudWatch Logs ──────────────────────────
# Requeridos cuando cuopt_self_hosted=true (sin NAT Gateway).
# ECS Fargate necesita acceder a ECR para pull de imágenes y a CloudWatch Logs
# para el log driver awslogs. Sin NAT ni estos endpoints, los tasks fallan con
# "ResourceInitializationError: unable to pull secrets or registry auth".
#
# Se crean siempre (no solo cuando cuopt_self_hosted=true) porque también
# benefician al ECS OSRM — reducen latencia y evitan depender de NAT para pulls.

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${local.region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${local.name_prefix}-endpoint-ecr-api" }
}

resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${local.region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${local.name_prefix}-endpoint-ecr-dkr" }
}

resource "aws_vpc_endpoint" "logs" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${local.region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${local.name_prefix}-endpoint-logs" }
}
