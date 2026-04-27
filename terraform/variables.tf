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
