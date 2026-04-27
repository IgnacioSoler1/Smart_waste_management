# ─────────────────────────────────────────────────────────
# AWS IoT Core — SmartWaste MVD
#
# Arquitectura MQTT:
#   Sensor simulator  ──MQTT──▶  IoT Core
#                                    │
#                              IoT Rule (próxima fase)
#                                    │
#                              Lambda process-sensor-reading
#                                    │
#                              DynamoDB sensor-readings
#
# Topic scheme:
#   smartwaste-dev/sensors/{container_id}           ← sensores publican
#   smartwaste-dev/trucks/{truck_id}/position       ← GPS publica
#   smartwaste-dev/routes/{truck_id}/current        ← driver app suscribe
# ─────────────────────────────────────────────────────────

# ── Tipo de thing: contenedor ─────────────────────────────
# Agrupa todos los dispositivos IoT que representan contenedores.
# Permite filtrar y buscar things por tipo en la consola AWS
# y en el simulador.
resource "aws_iot_thing_type" "waste_container" {
  name = "${local.name_prefix}-WasteContainer"

  properties {
    description = "Contenedor de residuos domiciliarios con sensor simulado de nivel de llenado"

    searchable_attributes = [
      "circuit_id",
      "zone",
      "shift",
    ]
  }

  tags = {
    Name = "${local.name_prefix}-WasteContainer"
  }
}

# ── Política IoT para sensores ────────────────────────────
# Política mínima para que los simuladores (y en producción,
# los dispositivos físicos) puedan:
#   - Conectarse con un clientId con prefijo "smartwaste-"
#   - Publicar lecturas en topics smartwaste/containers/*
#   - Suscribirse y recibir mensajes en topics smartwaste/*
#     (para futuros comandos enviados al dispositivo)
#
# Principio de mínimo privilegio:
#   - Connect restringido por prefijo de clientId
#   - Publish restringido al sub-tree containers/
#   - Subscribe/Receive al tree completo smartwaste/
resource "aws_iot_policy" "sensor_policy" {
  name = "${local.name_prefix}-sensor-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowConnect"
        Effect = "Allow"
        Action = "iot:Connect"
        Resource = [
          "arn:aws:iot:${local.region}:${local.account_id}:client/${local.name_prefix}-*"
        ]
      },
      {
        Sid    = "AllowPublishSensorReadings"
        Effect = "Allow"
        Action = [
          "iot:Publish",
          "iot:RetainPublish",
        ]
        Resource = [
          "arn:aws:iot:${local.region}:${local.account_id}:topic/${local.name_prefix}/sensors/*",
          "arn:aws:iot:${local.region}:${local.account_id}:topic/${local.name_prefix}/trucks/*/position",
        ]
      },
      {
        Sid    = "AllowSubscribeAndReceive"
        Effect = "Allow"
        Action = [
          "iot:Subscribe",
          "iot:Receive",
        ]
        Resource = [
          # Subscribe usa topicfilter ARN
          "arn:aws:iot:${local.region}:${local.account_id}:topicfilter/${local.name_prefix}/*",
          # Receive usa topic ARN
          "arn:aws:iot:${local.region}:${local.account_id}:topic/${local.name_prefix}/*",
        ]
      },
    ]
  })

  tags = {
    Name = "${local.name_prefix}-sensor-policy"
  }
}

# ── Endpoint IoT (dato de referencia) ────────────────────
# Expone el endpoint ATS (Amazon Trust Services) de IoT Core
# para esta cuenta/región. Se usa en los outputs y en el
# simulador (variable IOT_ENDPOINT).
data "aws_iot_endpoint" "ats" {
  endpoint_type = "iot:Data-ATS"
}
