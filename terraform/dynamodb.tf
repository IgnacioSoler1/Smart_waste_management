# ─────────────────────────────────────────────────────────
# DynamoDB — tablas operativas de SmartWaste MVD
#
# Todas las tablas usan PAY_PER_REQUEST (on-demand):
#   - Sin capacidad provisionada que dimensionar
#   - Escala automáticamente con el tráfico de los sensores
#   - Costo cero cuando no hay lecturas (entorno dev)
#
# Nombres de tabla: "${local.name_prefix}-<entidad>"
#   dev  → smartwaste-dev-containers
#   prod → smartwaste-containers
# ─────────────────────────────────────────────────────────

# ── Contenedores ──────────────────────────────────────────
# Guarda metadatos estáticos de cada contenedor: ubicación,
# circuito, capacidad, estado. Se puebla desde el script
# data/scripts/seed_db.py.
#
# Accesos frecuentes:
#   - Por container_id (primary key) → lectura de estado por la Lambda
#   - Por circuit_id (GSI) → route-optimizer lee todos los contenedores
#     de un circuito de una sola query
resource "aws_dynamodb_table" "containers" {
  name         = "${local.name_prefix}-containers"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "container_id"

  attribute {
    name = "container_id"
    type = "S"
  }

  attribute {
    name = "circuit_id"
    type = "S"
  }

  global_secondary_index {
    name            = "circuit-index"
    hash_key        = "circuit_id"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = var.environment == "prod"
  }

  tags = {
    Name = "${local.name_prefix}-containers"
  }
}

# ── Camiones ──────────────────────────────────────────────
# Estado operativo de cada camión: circuito asignado,
# posición GPS actual, capacidad restante, estado (active /
# en_route / maintenance).
#
# Accesos frecuentes:
#   - Por truck_id (primary key) → actualizaciones de posición GPS
#   - Por status (GSI) → dispatcher consulta camiones disponibles
resource "aws_dynamodb_table" "trucks" {
  name         = "${local.name_prefix}-trucks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "truck_id"

  attribute {
    name = "truck_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = var.environment == "prod"
  }

  tags = {
    Name = "${local.name_prefix}-trucks"
  }
}

# ── Rutas ─────────────────────────────────────────────────
# Rutas calculadas por el route-optimizer. Cada ítem contiene
# la secuencia de contenedores a visitar, distancia total y
# ventanas de tiempo estimadas.
#
# Accesos frecuentes:
#   - Por route_id (primary key) → GET /routes/{id}, driver app
#   - Por circuit_id (GSI) → _supersede_routes y GET /circuits/{id}/route
#
# La asociación truck → ruta activa se resuelve sin GSI: el optimizer
# escribe active_route_id directamente en la tabla trucks tras guardar
# cada ruta. El driver app hace GET /trucks/{id} y luego GET /routes/{id}.
resource "aws_dynamodb_table" "routes" {
  name         = "${local.name_prefix}-routes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "route_id"

  attribute {
    name = "route_id"
    type = "S"
  }

  attribute {
    name = "circuit_id"
    type = "S"
  }

  # GSI por circuit_id: permite Query eficiente de rutas activas por circuito
  # sin Scan de toda la tabla. Usado por _supersede_routes y GET /circuits/{id}/route.
  global_secondary_index {
    name            = "circuit-index"
    hash_key        = "circuit_id"
    projection_type = "ALL"
  }

  # TTL en el atributo "expires_at" (epoch seconds).
  # Las rutas superseded se auto-borran a los 7 días.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.environment == "prod"
  }

  tags = {
    Name = "${local.name_prefix}-routes"
  }
}

# ── Lecturas de sensores ──────────────────────────────────
# Time-series de lecturas de nivel de llenado. PK compuesta
# para soportar queries del tipo "dame las últimas N lecturas
# del contenedor X".
#
# TTL en el atributo "ttl" (epoch seconds): la Lambda que
# procesa cada lectura calcula ttl = now + 30 días. Lecturas
# más antiguas se borran automáticamente por DynamoDB.
# El historial a largo plazo se guarda en S3 vía Kinesis
# Firehose (ver architecture.md).
#
# Accesos frecuentes:
#   - Por container_id + timestamp (primary key) → lectura de
#     nivel actual (última lectura por contenedor)
resource "aws_dynamodb_table" "sensor_readings" {
  name         = "${local.name_prefix}-sensor-readings"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "container_id"
  range_key    = "timestamp"

  attribute {
    name = "container_id"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.environment == "prod"
  }

  tags = {
    Name = "${local.name_prefix}-sensor-readings"
  }
}
