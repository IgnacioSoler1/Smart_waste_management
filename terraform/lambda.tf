# ─────────────────────────────────────────────────────────
# Lambda — SmartWaste MVD
#
# Lambdas implementadas:
#   - process-sensor-reading  : IoT Rule → SQS → Lambda (batch) → DynamoDB
#   - route-optimizer         : EventBridge (cada 15 min) → OSRM + cuOpt/OR-Tools
#
# Lambdas implementadas (Fase 3):
#   - websocket-connect       : API GW WebSocket $connect    → websocket.tf
#   - websocket-disconnect    : API GW WebSocket $disconnect → websocket.tf
#   - websocket-message       : API GW WebSocket container_emptied → websocket.tf
# ─────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────
# SQS — Cola de lecturas de sensores
#
# Pipeline: IoT Core → SQS → Lambda (batch, 100 msgs/invocación)
#
# Beneficio: el simulator publica ~10,937 msgs cada 10 min. Sin SQS,
# IoT Rule dispararía ~10,937 invocaciones Lambda concurrentes, agotando
# el límite de concurrencia de la cuenta (~1,000 por defecto). Con SQS,
# el event source mapping dispara ~110 invocaciones batch de hasta 100 msgs.
# ─────────────────────────────────────────────────────────

resource "aws_sqs_queue" "sensor_readings" {
  name = "${local.name_prefix}-sensor-readings"

  # visibility_timeout >= Lambda timeout para evitar que un mensaje vuelva
  # a ser visible mientras la Lambda aún lo está procesando.
  visibility_timeout_seconds = 60

  # 1 hora: si la Lambda falla repetidamente, los mensajes se descartan
  # en vez de acumularse indefinidamente. Los datos llegan también a Kinesis
  # Firehose (S3), así que no hay pérdida de datos históricos.
  message_retention_seconds = 3600

  # Long polling: reduce llamadas vacías al SQS cuando hay poca actividad.
  receive_wait_time_seconds = 20

  tags = {
    Name = "${local.name_prefix}-sensor-readings-queue"
  }
}

# Permite a IoT Core escribir en la cola.
# Restringido al ARN de la regla específica para evitar que otras reglas
# (o recursos con el mismo rol) publiquen mensajes no autorizados.
resource "aws_sqs_queue_policy" "sensor_readings_iot" {
  queue_url = aws_sqs_queue.sensor_readings.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "iot.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.sensor_readings.arn
      Condition = {
        ArnLike = {
          "aws:SourceArn" = aws_iot_topic_rule.smartwaste_sensor_ingest.arn
        }
      }
    }]
  })
}


# ─────────────────────────────────────────────────────────
# process-sensor-reading
# ─────────────────────────────────────────────────────────

# Empaqueta el handler en un ZIP para que Terraform lo suba a Lambda.
# Cada vez que cambie handler.py, Terraform detecta el cambio en el hash
# del ZIP y re-despliega automáticamente.
data "archive_file" "process_sensor_reading" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/process-sensor-reading"
  output_path = "${path.module}/.terraform/process-sensor-reading.zip"
}

# ── IAM ──────────────────────────────────────────────────

# Rol de ejecución de la Lambda
resource "aws_iam_role" "process_sensor_reading" {
  name = "${local.name_prefix}-process-sensor-reading"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Permiso para escribir logs en CloudWatch (grupo creado automáticamente)
resource "aws_iam_role_policy_attachment" "process_sensor_reading_logs" {
  role       = aws_iam_role.process_sensor_reading.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# DynamoDB: UpdateItem en containers (última lectura) + PutItem en sensor_readings (time-series).
resource "aws_iam_role_policy" "process_sensor_reading_dynamo" {
  name = "dynamodb-update-containers"
  role = aws_iam_role.process_sensor_reading.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "UpdateContainerState"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.containers.arn
      },
      {
        Sid      = "PutSensorReadingTimeSeries"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.sensor_readings.arn
      },
    ]
  })
}

# SQS: recibir, borrar y leer atributos de la cola.
# Requerido por el event source mapping de Lambda.
resource "aws_iam_role_policy" "process_sensor_reading_sqs" {
  name = "sqs-consume-sensor-readings"
  role = aws_iam_role.process_sensor_reading.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
      ]
      Resource = aws_sqs_queue.sensor_readings.arn
    }]
  })
}

# ── Lambda function ───────────────────────────────────────

resource "aws_lambda_function" "process_sensor_reading" {
  function_name    = "${local.name_prefix}-process-sensor-reading"
  description      = "Procesa lecturas de sensores IoT y actualiza el estado de los contenedores en DynamoDB"
  role             = aws_iam_role.process_sensor_reading.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.process_sensor_reading.output_path
  source_code_hash = data.archive_file.process_sensor_reading.output_base64sha256

  # 256 MB: batches de 100 msgs con ThreadPoolExecutor necesitan más RAM que invocaciones individuales.
  # 60 s: alineado con visibility_timeout_seconds de la SQS queue.
  # El peor caso (100 updates DynamoDB en serie) tarda < 2 s; 60 s es margen amplio.
  memory_size = 256
  timeout     = 60

  environment {
    variables = {
      CONTAINERS_TABLE      = aws_dynamodb_table.containers.name
      SENSOR_READINGS_TABLE = aws_dynamodb_table.sensor_readings.name
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.process_sensor_reading_logs,
    aws_iam_role_policy.process_sensor_reading_dynamo,
    aws_iam_role_policy.process_sensor_reading_sqs,
  ]
}

# SQS event source mapping: dispara la Lambda cuando hay mensajes en la cola.
# batch_size=100: maximiza el agrupamiento para reducir invocaciones.
# batching_window=10s: acumula mensajes hasta 10 s o hasta 100 — lo que ocurra primero.
# ReportBatchItemFailures: la Lambda devuelve solo los messageIds fallidos;
# el resto se confirman automáticamente sin necesidad de reintentar el batch completo.
resource "aws_lambda_event_source_mapping" "sensor_sqs" {
  event_source_arn                   = aws_sqs_queue.sensor_readings.arn
  function_name                      = aws_lambda_function.process_sensor_reading.arn
  batch_size                         = 100
  maximum_batching_window_in_seconds = 10
  function_response_types            = ["ReportBatchItemFailures"]

  depends_on = [aws_iam_role_policy.process_sensor_reading_sqs]
}

# Grupo de logs explícito con retención de 14 días.
# Sin este recurso, CloudWatch crearía el grupo automáticamente sin TTL.
resource "aws_cloudwatch_log_group" "process_sensor_reading" {
  name              = "/aws/lambda/${aws_lambda_function.process_sensor_reading.function_name}"
  retention_in_days = 14
}

# ── IoT Rule ─────────────────────────────────────────────

# Rol que IoT Core usa para invocar la Lambda.
# IoT Rules necesitan su propio rol IAM (no usan el rol de la Lambda).
resource "aws_iam_role" "iot_sensor_ingest" {
  name = "${local.name_prefix}-iot-sensor-ingest"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "iot.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "iot_sensor_ingest_sqs" {
  name = "sqs-send-sensor-readings"
  role = aws_iam_role.iot_sensor_ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.sensor_readings.arn
    }]
  })
}

# CloudWatch Logs: necesario para que la error_action del IoT Rule pueda
# escribir errores de evaluación de la regla. Sin este permiso la acción
# de error falla silenciosamente y los errores se pierden.
resource "aws_iam_role_policy" "iot_sensor_ingest_logs" {
  name = "cloudwatch-logs-error-action"
  role = aws_iam_role.iot_sensor_ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
      ]
      Resource = "${aws_cloudwatch_log_group.iot_sensor_errors.arn}:*"
    }]
  })
}

# Log group explícito para errores de la IoT Rule.
# Sin este recurso, IoT Core intentaría crear el grupo automáticamente
# pero podría fallar si el rol no tiene logs:CreateLogGroup en *.
resource "aws_cloudwatch_log_group" "iot_sensor_errors" {
  name              = "/aws/iot/${local.name_prefix}-sensor-ingest-errors"
  retention_in_days = 14

  tags = {
    Name = "${local.name_prefix}-iot-sensor-errors"
  }
}

# La regla escucha todos los topics smartwaste/<env>/sensors/+
# (el + es un wildcard MQTT de un nivel, captura cualquier container_id).
# SELECT * pasa el payload completo del mensaje al handler sin transformar.
resource "aws_iot_topic_rule" "smartwaste_sensor_ingest" {
  name        = replace("${local.name_prefix}_sensor_ingest", "-", "_")
  description = "Reenvía lecturas de sensores IoT a la Lambda process-sensor-reading"
  enabled     = true
  sql         = "SELECT * FROM '${local.name_prefix}/sensors/+'"
  sql_version = "2016-03-23"

  sqs {
    queue_url  = aws_sqs_queue.sensor_readings.url
    role_arn   = aws_iam_role.iot_sensor_ingest.arn
    use_base64 = false
  }

  # Segunda acción: reenviar la lectura raw al Data Lake vía Kinesis Data Stream.
  # IoT Core escribe en paralelo a SQS Y a Kinesis — no hay dependencia entre sí.
  # $${container_id} es un template de sustitución de IoT (no interpolación Terraform).
  kinesis {
    stream_name   = aws_kinesis_stream.sensor_stream.name
    partition_key = "$${container_id}"
    role_arn      = aws_iam_role.iot_kinesis_ingest.arn
  }

  # Si la regla tiene un error de evaluación, loguear en CloudWatch.
  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.iot_sensor_errors.name
      role_arn       = aws_iam_role.iot_sensor_ingest.arn
    }
  }
}


# ─────────────────────────────────────────────────────────
# route-optimizer
# ─────────────────────────────────────────────────────────

# ── Build del paquete de despliegue ───────────────────────
#
# build.sh hace tres cosas:
#   1. Copia handler.py + módulos de cuopt-client/ al BUILD_DIR
#   2. Instala requests y ortools para linux/x86_64 (plataforma de Lambda)
#
# Se re-ejecuta automáticamente cuando cambia cualquier archivo fuente.
#
# PRIMERA VEZ: el directorio .build/route-optimizer/ no existe hasta que
# se corra el apply. Terraform ejecutará primero null_resource (apply) y
# luego archive_file leerá el directorio ya poblado. Si se necesita hacer
# plan antes del primer apply, correr manualmente:
#   bash lambdas/route-optimizer/build.sh
resource "null_resource" "route_optimizer_build" {
  triggers = {
    handler     = filemd5("${path.module}/../lambdas/route-optimizer/handler.py")
    reqs        = filemd5("${path.module}/../lambdas/route-optimizer/requirements.txt")
    build_sh    = filemd5("${path.module}/../lambdas/route-optimizer/build.sh")
    osrm_client = filemd5("${path.module}/../cuopt-client/osrm_client.py")
    vrp_solver  = filemd5("${path.module}/../cuopt-client/vrp_solver.py")
    constraints = filemd5("${path.module}/../cuopt-client/constraints.py")
    ws_notifier = filemd5("${path.module}/../lambdas/shared/ws_notifier.py")
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/../lambdas/route-optimizer/build.sh ${path.module}/.build/route-optimizer"
  }
}

# Zip del build dir (poblado por null_resource anterior)
data "archive_file" "route_optimizer" {
  type        = "zip"
  source_dir  = "${path.module}/.build/route-optimizer"
  output_path = "${path.module}/.build/route-optimizer.zip"
  depends_on  = [null_resource.route_optimizer_build]
}

# Upload del zip a S3 — necesario porque ortools > 50 MB (límite de upload directo).
# La Lambda lee desde S3 en vez del API de Lambda.
resource "aws_s3_object" "route_optimizer_zip" {
  bucket = aws_s3_bucket.data_lake.bucket
  key    = "terraform-artifacts/lambdas/route-optimizer.zip"
  source = data.archive_file.route_optimizer.output_path
  etag   = data.archive_file.route_optimizer.output_md5

  depends_on = [null_resource.route_optimizer_build]
}

# ── IAM ──────────────────────────────────────────────────

resource "aws_iam_role" "route_optimizer" {
  name = "${local.name_prefix}-route-optimizer"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "route_optimizer_logs" {
  role       = aws_iam_role.route_optimizer.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "route_optimizer_dynamo" {
  name = "dynamodb-access"
  role = aws_iam_role.route_optimizer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadContainers"
        Effect = "Allow"
        # Query por GSI circuit-index + Scan para modo "todos los circuitos del turno"
        Action = ["dynamodb:Query", "dynamodb:Scan"]
        Resource = [
          aws_dynamodb_table.containers.arn,
          "${aws_dynamodb_table.containers.arn}/index/circuit-index",
        ]
      },
      {
        Sid    = "ReadTrucks"
        Effect = "Allow"
        Action = ["dynamodb:Query"]
        Resource = [
          aws_dynamodb_table.trucks.arn,
          "${aws_dynamodb_table.trucks.arn}/index/status-index",
        ]
      },
      {
        Sid    = "WriteRoutes"
        Effect = "Allow"
        # PutItem para nuevas rutas, UpdateItem para superseder anteriores, Query por GSI circuit-index
        Action = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = [
          aws_dynamodb_table.routes.arn,
          "${aws_dynamodb_table.routes.arn}/index/circuit-index",
        ]
      },
      {
        Sid    = "UpdateTruckActiveRoute"
        Effect = "Allow"
        # UpdateItem para escribir active_route_id en el camión tras guardar cada ruta
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.trucks.arn
      },
    ]
  })
}

# Permiso para notificar conductores vía WebSocket Management API + leer conexiones
resource "aws_iam_role_policy" "route_optimizer_websocket" {
  name = "websocket-notify"
  role = aws_iam_role.route_optimizer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ManagementAPI"
        Effect = "Allow"
        # post_to_connection para enviar rutas a conductores conectados
        Action   = ["execute-api:ManageConnections"]
        Resource = "${aws_apigatewayv2_stage.ws_dev.execution_arn}/POST/@connections/*"
      },
      {
        Sid    = "ReadConnections"
        Effect = "Allow"
        # Query del GSI circuit-index para encontrar conexiones del circuito
        Action   = ["dynamodb:Query"]
        Resource = "${aws_dynamodb_table.connections.arn}/index/circuit-index"
      },
      {
        Sid    = "DeleteStaleConnections"
        Effect = "Allow"
        # Eliminar conexiones stale (GoneException) sin necesitar la Lambda $disconnect
        Action   = ["dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.connections.arn
      },
    ]
  })
}

# Permiso para leer el zip de despliegue desde S3
# Permite que la Lambda cree/destruya ENIs en la VPC (requerido para vpc_config)
resource "aws_iam_role_policy_attachment" "route_optimizer_vpc" {
  role       = aws_iam_role.route_optimizer.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "route_optimizer_s3" {
  name = "s3-read-artifact"
  role = aws_iam_role.route_optimizer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadLambdaZip"
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "${aws_s3_bucket.data_lake.arn}/terraform-artifacts/lambdas/*"
    }]
  })
}

# Permiso para publicar resúmenes de rutas a Kinesis Firehose (Bronze layer).
# Firehose bufferiza los registros y escribe archivos más grandes a S3.
resource "aws_iam_role_policy" "route_optimizer_firehose" {
  name = "firehose-put-route-results"
  role = aws_iam_role.route_optimizer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "PutRouteResults"
      Effect   = "Allow"
      Action   = ["firehose:PutRecord"]
      Resource = aws_kinesis_firehose_delivery_stream.route_firehose.arn
    }]
  })
}

# ── Lambda function ───────────────────────────────────────

resource "aws_lambda_function" "route_optimizer" {
  function_name = "${local.name_prefix}-route-optimizer"
  description   = "Optimiza rutas de recolección por circuito usando OSRM + cuOpt/OR-Tools"
  role          = aws_iam_role.route_optimizer.arn
  runtime       = "python3.11"
  handler       = "handler.lambda_handler"

  # Carga el zip desde S3 (no puede ser filename porque ortools > 50 MB)
  s3_bucket        = aws_s3_object.route_optimizer_zip.bucket
  s3_key           = aws_s3_object.route_optimizer_zip.key
  source_code_hash = data.archive_file.route_optimizer.output_base64sha256

  # 512 MB: OR-Tools necesita memoria para el grafo VRP. cuOpt en api_catalog
  # no usa memoria local, pero mantener margen por las matrices de distancias.
  # 300 s: OSRM Table API para ~100 contenedores tarda hasta 30 s; cuOpt/OR-Tools
  # puede tardar hasta 60 s en circuitos complejos. 300 s cubre casos extremos
  # (red lenta, circuitos grandes, reintentos) sin bloquear el EventBridge schedule.
  memory_size = 512
  timeout     = 300

  environment {
    variables = {
      CONTAINERS_TABLE = aws_dynamodb_table.containers.name
      TRUCKS_TABLE     = aws_dynamodb_table.trucks.name
      ROUTES_TABLE     = aws_dynamodb_table.routes.name
      OSRM_URL         = var.osrm_url
      OSRM_FALLBACK    = var.osrm_url == "http://localhost:5000" ? "haversine" : ""
      CUOPT_MODE       = var.cuopt_self_hosted ? "self_hosted" : var.cuopt_mode
      CUOPT_API_KEY    = var.cuopt_self_hosted ? "" : var.cuopt_api_key
      CUOPT_SERVER_URL = var.cuopt_self_hosted ? "http://cuopt.smartwaste.local:5000" : var.cuopt_server_url
      # WebSocket: para que ws_notifier pueda notificar conductores tras optimizar
      CONNECTIONS_TABLE = aws_dynamodb_table.connections.name
      WS_ENDPOINT       = "https://${aws_apigatewayv2_api.smartwaste_ws.id}.execute-api.${local.region}.amazonaws.com/dev"
      # Data Lake: publicar resumen de rutas en Firehose (bufferiza → S3 Bronze)
      ROUTE_RESULTS_FIREHOSE = aws_kinesis_firehose_delivery_stream.route_firehose.name
    }
  }

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.route_optimizer_lambda.id]
  }

  depends_on = [
    aws_iam_role_policy_attachment.route_optimizer_logs,
    aws_iam_role_policy_attachment.route_optimizer_vpc,
    aws_iam_role_policy.route_optimizer_dynamo,
    aws_iam_role_policy.route_optimizer_websocket,
    aws_s3_object.route_optimizer_zip,
    aws_nat_gateway.main, # conditional (count=0 when cuopt_self_hosted=true)
    aws_instance.cuopt,   # conditional (count=0 when cuopt_self_hosted=false)
  ]
}

resource "aws_cloudwatch_log_group" "route_optimizer" {
  name              = "/aws/lambda/${aws_lambda_function.route_optimizer.function_name}"
  retention_in_days = 14
}

# ── EventBridge — trigger cada 15 minutos ────────────────

resource "aws_cloudwatch_event_rule" "route_optimizer_schedule" {
  name                = "${local.name_prefix}-route-optimizer-schedule"
  description         = "Dispara el route-optimizer cada 15 minutos para recalcular rutas activas"
  schedule_expression = "rate(15 minutes)"
}

resource "aws_cloudwatch_event_target" "route_optimizer" {
  rule      = aws_cloudwatch_event_rule.route_optimizer_schedule.name
  target_id = "InvokeRouteOptimizer"
  arn       = aws_lambda_function.route_optimizer.arn
}

resource "aws_lambda_permission" "eventbridge_invoke_route_optimizer" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.route_optimizer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.route_optimizer_schedule.arn
}


# ─────────────────────────────────────────────────────────
# sensor-simulator
#
# Simula sensores IoT: calcula fill levels y publica a IoT Core
# cada 10 min vía EventBridge. Reemplaza al simulador CLI local.
# ─────────────────────────────────────────────────────────

# ── Build ────────────────────────────────────────────────

resource "null_resource" "sensor_simulator_build" {
  triggers = {
    handler      = filemd5("${path.module}/../lambdas/sensor-simulator/handler.py")
    build_sh     = filemd5("${path.module}/../lambdas/sensor-simulator/build.sh")
    fill_model   = filemd5("${path.module}/../simulator/fill_model.py")
    zone_density = filemd5("${path.module}/../simulator/zone_density.py")
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/../lambdas/sensor-simulator/build.sh ${path.module}/.build/sensor-simulator"
  }
}

data "archive_file" "sensor_simulator" {
  type        = "zip"
  source_dir  = "${path.module}/.build/sensor-simulator"
  output_path = "${path.module}/.build/sensor-simulator.zip"
  depends_on  = [null_resource.sensor_simulator_build]
}

# ── IAM ──────────────────────────────────────────────────

resource "aws_iam_role" "sensor_simulator" {
  name = "${local.name_prefix}-sensor-simulator"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "sensor_simulator_logs" {
  role       = aws_iam_role.sensor_simulator.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "sensor_simulator_dynamo" {
  name = "dynamodb-read-containers"
  role = aws_iam_role.sensor_simulator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:Scan"]
      Resource = aws_dynamodb_table.containers.arn
    }]
  })
}

resource "aws_iam_role_policy" "sensor_simulator_iot_publish" {
  name = "iot-publish-sensor-readings"
  role = aws_iam_role.sensor_simulator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["iot:Publish"]
      Resource = "arn:aws:iot:${local.region}:${local.account_id}:topic/${local.name_prefix}/sensors/*"
    }]
  })
}

# ── Lambda function ───────────────────────────────────────

resource "aws_lambda_function" "sensor_simulator" {
  function_name    = "${local.name_prefix}-sensor-simulator"
  description      = "Simula sensores IoT: calcula fill levels y publica lecturas a IoT Core"
  role             = aws_iam_role.sensor_simulator.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.sensor_simulator.output_path
  source_code_hash = data.archive_file.sensor_simulator.output_base64sha256

  # 10,937 contenedores × publish IoT → necesita más tiempo y memoria
  memory_size = 256
  timeout     = 300 # 5 min max (EventBridge cada 10 min)

  environment {
    variables = {
      CONTAINERS_TABLE = aws_dynamodb_table.containers.name
      IOT_TOPIC_PREFIX = "${local.name_prefix}/sensors"
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.sensor_simulator_logs,
    aws_iam_role_policy.sensor_simulator_dynamo,
    aws_iam_role_policy.sensor_simulator_iot_publish,
  ]
}

resource "aws_cloudwatch_log_group" "sensor_simulator" {
  name              = "/aws/lambda/${aws_lambda_function.sensor_simulator.function_name}"
  retention_in_days = 14
}

# ── EventBridge: cada 10 min ─────────────────────────────

resource "aws_cloudwatch_event_rule" "sensor_simulator_schedule" {
  name                = "${local.name_prefix}-sensor-simulator-schedule"
  description         = "Dispara el sensor-simulator cada 10 minutos para simular lecturas IoT"
  schedule_expression = "rate(10 minutes)"
}

resource "aws_cloudwatch_event_target" "sensor_simulator" {
  rule      = aws_cloudwatch_event_rule.sensor_simulator_schedule.name
  target_id = "InvokeSensorSimulator"
  arn       = aws_lambda_function.sensor_simulator.arn
}

resource "aws_lambda_permission" "eventbridge_invoke_sensor_simulator" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sensor_simulator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sensor_simulator_schedule.arn
}
