terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }

  # Backend local — migrar a S3 cuando se promueva a producción.
  # Ver README.md para instrucciones de migración.
  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "aws" {
  region  = "us-east-1"
  profile = "personal-smart-recycle"
  default_tags {
    tags = {
      Project     = var.project_prefix
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ─────────────────────────────────────────────────────────
# Data sources de contexto (cuenta y región actuales)
# ─────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─────────────────────────────────────────────────────────
# Locals
# ─────────────────────────────────────────────────────────

locals {
  # Prefijo para todos los recursos: "smartwaste" en prod, "smartwaste-dev" en dev, etc.
  # Las tablas DynamoDB usan este prefijo para separar entornos en la misma cuenta.
  name_prefix = var.environment == "prod" ? var.project_prefix : "${var.project_prefix}-${var.environment}"

  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
}
