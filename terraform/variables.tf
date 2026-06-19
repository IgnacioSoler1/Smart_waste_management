variable "environment" {
  description = "Nombre del entorno de despliegue. Determina el prefijo de los recursos y las tags."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "El entorno debe ser 'dev', 'staging' o 'prod'."
  }
}

variable "project_prefix" {
  description = "Prefijo base del proyecto. Se usa en los nombres de todos los recursos AWS."
  type        = string
  default     = "smartwaste"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,19}$", var.project_prefix))
    error_message = "El prefijo debe tener entre 3 y 20 caracteres: letras minúsculas, números y guiones, y empezar con letra."
  }
}

# ── IoT JITR ─────────────────────────────────────────────

variable "iot_ca_certificate_id" {
  description = "ID del certificado CA registrado en IoT Core. Se obtiene al ejecutar firmware/provisioning/register_ca.sh."
  type        = string
  default     = ""
}

# ── route-optimizer ───────────────────────────────────────

variable "osrm_url" {
  description = "URL base del servidor OSRM. Apunta al ECS Fargate via Cloud Map DNS."
  type        = string
  default     = "http://osrm.smartwaste.local:5000"
}

variable "cuopt_mode" {
  description = "Backend VRP: 'ortools' (CPU, sin costo), 'api_catalog' (NVIDIA API Catalog), 'self_hosted' (EC2 GPU)."
  type        = string
  default     = "ortools"

  validation {
    condition     = contains(["ortools", "api_catalog", "self_hosted"], var.cuopt_mode)
    error_message = "cuopt_mode debe ser 'ortools', 'api_catalog' o 'self_hosted'."
  }
}

variable "cuopt_api_key" {
  description = "API key del NVIDIA API Catalog. Solo requerida cuando cuopt_mode='api_catalog'. Se guarda como variable de entorno de la Lambda (no en tfstate)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cuopt_server_url" {
  description = "URL del servidor cuOpt self-hosted. Solo requerida cuando cuopt_mode='self_hosted'."
  type        = string
  default     = ""
}

# ─────────────────────────────────────────────────────────
# Multi-circuit consolidation (Vector 5.1 de docs/cuopt.md)
# ─────────────────────────────────────────────────────────

variable "max_locations_per_group" {
  description = "Cap de locations por solve consolidado en route-optimizer. OSRM client soporta hasta 5000 (server ECS hasta 10000). El binding real es cuOpt: cloud API admite ~1000 nodos por solve, self-hosted ~10000+. Default 1000 = límite de cloud API. Si cuopt_mode='self_hosted' se puede subir a 5000+. Bajar este número fuerza más grupos chicos (menos beneficio de consolidación)."
  type        = number
  default     = 1000
  validation {
    condition     = var.max_locations_per_group > 10 && var.max_locations_per_group <= 10000
    error_message = "max_locations_per_group debe estar entre 11 y 10000 (hard-limit OSRM server)."
  }
}

variable "min_circuits_for_consolidation" {
  description = "Cantidad mínima de circuitos en un grupo para activar el solve consolidado. Con 1 circuito se cae al path single-circuit original. Subirlo a 3+ deshabilita la consolidación para grupos de 2 (útil para A/B test conservador)."
  type        = number
  default     = 2
  validation {
    condition     = var.min_circuits_for_consolidation >= 1
    error_message = "min_circuits_for_consolidation debe ser ≥1."
  }
}


