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

# ── JITR (Just-in-Time Registration) ───────────────────
# Provisioning automático de dispositivos ESP32.
#
# Flujo:
#   1. Dispositivo nuevo conecta con cert firmado por la CA registrada
#   2. IoT Core auto-registra el cert (PENDING_ACTIVATION)
#   3. IoT Core publica en $aws/events/certificates/registered/<caCertId>
#   4. IoT Rule dispara Lambda jitr-provisioning
#   5. Lambda crea Thing, activa cert, attacha policy
#   6. Dispositivo reconecta en siguiente boot → funciona
#
# La CA se registra manualmente con:
#   cd firmware/provisioning && ./register_ca.sh
#
# La variable var.iot_ca_certificate_id se obtiene del output de register_ca.sh
# y se configura en terraform.tfvars.

# ── Lambda JITR ────────────────────────────────────────

data "archive_file" "jitr_provisioning" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/jitr-provisioning"
  output_path = "${path.module}/.terraform/jitr-provisioning.zip"
}

resource "aws_iam_role" "jitr_lambda" {
  name = "${local.name_prefix}-jitr-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-jitr-lambda-role"
  }
}

resource "aws_iam_role_policy_attachment" "jitr_lambda_logs" {
  role       = aws_iam_role.jitr_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "jitr_lambda_iot" {
  name = "iot-provisioning-permissions"
  role = aws_iam_role.jitr_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "AllowJITRProvisionResources"
      Effect = "Allow"
      Action = [
        "iot:DescribeCertificate",
        "iot:UpdateCertificate",
        "iot:CreateThing",
        "iot:DescribeThing",
        "iot:AttachPolicy",
        "iot:AttachThingPrincipal",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_lambda_function" "jitr_provisioning" {
  function_name    = "${local.name_prefix}-jitr-provisioning"
  description      = "JITR: provisiona dispositivos ESP32 automáticamente en su primera conexión"
  role             = aws_iam_role.jitr_lambda.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.jitr_provisioning.output_path
  source_code_hash = data.archive_file.jitr_provisioning.output_base64sha256
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      IOT_POLICY_NAME = aws_iot_policy.sensor_policy.name
      IOT_THING_TYPE  = aws_iot_thing_type.waste_container.name
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.jitr_lambda_logs,
    aws_iam_role_policy.jitr_lambda_iot,
  ]
}

resource "aws_cloudwatch_log_group" "jitr_provisioning" {
  name              = "/aws/lambda/${aws_lambda_function.jitr_provisioning.function_name}"
  retention_in_days = 14
}

# ── IoT Rule: certificate registered → Lambda ─────────

resource "aws_lambda_permission" "jitr_iot_invoke" {
  statement_id  = "AllowIoTInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.jitr_provisioning.function_name
  principal     = "iot.amazonaws.com"
}

resource "aws_iot_topic_rule" "jitr_provision" {
  count = var.iot_ca_certificate_id != "" ? 1 : 0

  name        = "${replace(local.name_prefix, "-", "_")}_jitr_provision"
  enabled     = true
  sql         = "SELECT * FROM '$aws/events/certificates/registered/${var.iot_ca_certificate_id}'"
  sql_version = "2016-03-23"

  lambda {
    function_arn = aws_lambda_function.jitr_provisioning.arn
  }

  tags = {
    Name = "${local.name_prefix}-jitr-provision"
  }
}

# ── Endpoint IoT (dato de referencia) ────────────────────
# Expone el endpoint ATS (Amazon Trust Services) de IoT Core
# para esta cuenta/región. Se usa en los outputs y en el
# simulador (variable IOT_ENDPOINT).
data "aws_iot_endpoint" "ats" {
  endpoint_type = "iot:Data-ATS"
}
