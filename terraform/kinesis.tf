# ─────────────────────────────────────────────────────────
# Data Lake pipeline — SmartWaste MVD
#
# Flujo:
#   IoT Core  ──IoT Rule──▶  Kinesis Data Stream
#                                    │
#                            Kinesis Firehose
#                                    │
#                            S3 (sensor-readings/year=.../...)
#
# Glue + Athena se configurarán en una fase posterior.
# ─────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────
# S3 — Data Lake bucket
# ─────────────────────────────────────────────────────────

# El nombre incluye el environment para separar dev/prod sin necesitar cuentas distintas.
# Nota: los nombres de bucket S3 son globales — si hay colisión cambiar a
# "${local.name_prefix}-data-lake-${local.account_id}".
resource "aws_s3_bucket" "data_lake" {
  bucket = "smartwaste-data-lake-${var.environment}"

  tags = {
    Name = "smartwaste-data-lake-${var.environment}"
  }
}

# Nunca debe ser accesible públicamente — bloquear todas las vías.
resource "aws_s3_bucket_public_access_block" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Cifrado en reposo con SSE-S3 (AES-256).
# Suficiente para datos operativos no-PII; migrar a SSE-KMS si se
# añaden datos sensibles de usuarios.
resource "aws_s3_bucket_server_side_encryption_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Lifecycle consolidado para todo el bucket.
# IMPORTANTE: solo puede existir un aws_s3_bucket_lifecycle_configuration por bucket.
# Múltiples recursos sobre el mismo bucket se sobreescriben silenciosamente.
resource "aws_s3_bucket_lifecycle_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  # Bronze — Sensor readings (Firehose): alta frecuencia de escritura, baja de lectura.
  # IA a 30 días reduce costos ~40 %. Historial > 1 año está en S3 Parquet Silver.
  rule {
    id     = "sensor-readings-tiering"
    status = "Enabled"

    filter {
      prefix = "sensor-readings/"
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = 365
    }
  }

  # Bronze — Route results (Firehose direct put): ~15 rutas/hora, volumen bajo.
  # Mismo ciclo que sensor-readings: IA a 30d, expirar a 365d.
  rule {
    id     = "route-results-tiering"
    status = "Enabled"

    filter {
      prefix = "route-results/"
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = 365
    }
  }

  # Silver — Sensor readings Parquet (Glue ETL): acceso frecuente durante 60d (Athena
  # queries recientes), luego archivo. Retener 2 años para análisis histórico.
  rule {
    id     = "sensor-readings-parquet-tiering"
    status = "Enabled"

    filter {
      prefix = "sensor-readings-parquet/"
    }

    transition {
      days          = 60
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = 730
    }
  }

  # Silver — Route results Parquet (Glue ETL)
  rule {
    id     = "route-results-parquet-tiering"
    status = "Enabled"

    filter {
      prefix = "route-results-parquet/"
    }

    transition {
      days          = 60
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = 730
    }
  }

  # Gold — Analytics results (JSON output de Glue ETL): dashboard los lee ~1 vez/día.
  # IA a 30d, expirar a 365d — se regeneran con cada run diario.
  rule {
    id     = "analytics-results-tiering"
    status = "Enabled"

    filter {
      prefix = "analytics-results/"
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = 365
    }
  }

  # Athena query results: efímeros por naturaleza (solo para la sesión de query).
  # Expirar a 7 días — el usuario ya leyó los resultados o son descartables.
  rule {
    id     = "athena-results-cleanup"
    status = "Enabled"

    filter {
      prefix = "athena-results/"
    }

    expiration {
      days = 7
    }
  }
}


# ─────────────────────────────────────────────────────────
# Kinesis Data Stream
# ─────────────────────────────────────────────────────────

# ON_DEMAND: escala automáticamente con la carga. El simulator publica ~10,937 msg
# cada 10 minutos (carga pico) pero la mayor parte del tiempo el stream está idle.
# ON_DEMAND elimina el costo fijo de shard (~$0.015/h × 720h = $10.80/mes) y cobra
# solo por mensajes reales (~$0.04/1M msgs). Con el volumen actual: ~$1.88/mes.
resource "aws_kinesis_stream" "sensor_stream" {
  name             = "${local.name_prefix}-sensor-stream"
  retention_period = 24 # horas — suficiente para re-procesar un día si Firehose cae

  stream_mode_details {
    stream_mode = "ON_DEMAND"
  }

  tags = {
    Name = "${local.name_prefix}-sensor-stream"
  }
}


# ─────────────────────────────────────────────────────────
# IAM — Firehose → (Kinesis + S3)
# ─────────────────────────────────────────────────────────

resource "aws_iam_role" "firehose_sensor" {
  name = "${local.name_prefix}-firehose-sensor"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # ExternalId restringe el trust a nuestra cuenta — previene confused deputy.
      Condition = {
        StringEquals = { "sts:ExternalId" = local.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "firehose_sensor" {
  name = "kinesis-read-s3-write"
  role = aws_iam_role.firehose_sensor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadFromKinesis"
        Effect = "Allow"
        Action = [
          "kinesis:GetRecords",
          "kinesis:GetShardIterator",
          "kinesis:DescribeStream",
          "kinesis:DescribeStreamSummary",
          "kinesis:ListShards",
          "kinesis:SubscribeToShard",
        ]
        Resource = aws_kinesis_stream.sensor_stream.arn
      },
      {
        Sid    = "WriteToS3"
        Effect = "Allow"
        Action = [
          "s3:AbortMultipartUpload",
          "s3:GetBucketLocation",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:ListBucketMultipartUploads",
          "s3:PutObject",
        ]
        Resource = [
          aws_s3_bucket.data_lake.arn,
          "${aws_s3_bucket.data_lake.arn}/*",
        ]
      },
    ]
  })
}


# ─────────────────────────────────────────────────────────
# Kinesis Firehose — Stream → S3
# ─────────────────────────────────────────────────────────

resource "aws_kinesis_firehose_delivery_stream" "sensor_firehose" {
  name        = "${local.name_prefix}-sensor-firehose"
  destination = "extended_s3"

  # Fuente: el Kinesis Data Stream (Firehose hace polling automático)
  kinesis_source_configuration {
    kinesis_stream_arn = aws_kinesis_stream.sensor_stream.arn
    role_arn           = aws_iam_role.firehose_sensor.arn
  }

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose_sensor.arn
    bucket_arn = aws_s3_bucket.data_lake.arn

    # Flush al llegar a 5 MB O a los 300 s (lo que ocurra primero).
    # En dev con tráfico bajo casi siempre flushea por tiempo.
    buffering_size     = 5   # MB
    buffering_interval = 300 # segundos

    compression_format = "GZIP"

    # Prefijo Hive-compatible: permite a Athena usar partition projection
    # sin necesitar un crawler de Glue para descubrir las particiones.
    prefix = "sensor-readings/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/"

    # Registros que Firehose no pudo entregar (ej: S3 throttling, schema error)
    error_output_prefix = "sensor-readings-errors/"
  }

  tags = {
    Name = "${local.name_prefix}-sensor-firehose"
  }
}


# ─────────────────────────────────────────────────────────
# Kinesis Firehose — Route Results (Bronze → S3)
#
# A diferencia del pipeline de sensores (IoT → Kinesis Data Stream → Firehose),
# las rutas tienen volumen muy bajo (~350 registros/día) y no necesitan
# el buffer de replay de un Data Stream. La Lambda escribe directamente
# a Firehose (direct put), que bufferiza y produce archivos más grandes.
#
# Costo estimado: ~$0.00 en dev (el tier gratuito de Firehose cubre
# 500 MB/mes; las rutas generan ~175 KB/día = ~5 MB/mes).
# ─────────────────────────────────────────────────────────

resource "aws_iam_role" "firehose_routes" {
  name = "${local.name_prefix}-firehose-routes"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "sts:ExternalId" = local.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "firehose_routes" {
  name = "s3-write-route-results"
  role = aws_iam_role.firehose_routes.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "WriteToS3"
      Effect = "Allow"
      Action = [
        "s3:AbortMultipartUpload",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:PutObject",
      ]
      Resource = [
        aws_s3_bucket.data_lake.arn,
        "${aws_s3_bucket.data_lake.arn}/route-results/*",
        "${aws_s3_bucket.data_lake.arn}/route-results-errors/*",
      ]
    }]
  })
}

# Direct put Firehose: la Lambda llama a firehose.put_record() directamente,
# sin pasar por un Kinesis Data Stream (no hace falta replay buffer para rutas).
resource "aws_kinesis_firehose_delivery_stream" "route_firehose" {
  name        = "${local.name_prefix}-route-firehose"
  destination = "extended_s3"

  # Sin kinesis_source_configuration → fuente: direct put desde Lambda

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose_routes.arn
    bucket_arn = aws_s3_bucket.data_lake.arn

    # Buffer por tiempo: con ~15 rutas/hora max, flushea casi siempre por tiempo
    # (el buffer de 5 MB nunca se llena a este volumen).
    # 900 s = 15 min = máximo permitido por Firehose = exactamente un ciclo de la
    # Lambda route-optimizer → todas las rutas del mismo ciclo en un solo archivo.
    buffering_size     = 5   # MB (fallback; casi nunca se alcanza)
    buffering_interval = 900 # segundos — máximo de Firehose (15 min)

    compression_format = "GZIP"

    # Mismo formato Hive que sensor-readings → partition projection funciona igual
    prefix              = "route-results/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/"
    error_output_prefix = "route-results-errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
  }

  tags = {
    Name = "${local.name_prefix}-route-firehose"
  }
}



# ─────────────────────────────────────────────────────────
# IAM — IoT Core → Kinesis Data Stream
# ─────────────────────────────────────────────────────────

# Rol separado del rol Lambda-invoke: cada acción de la IoT Rule
# puede (y debe) usar su propio rol de mínimo privilegio.
resource "aws_iam_role" "iot_kinesis_ingest" {
  name = "${local.name_prefix}-iot-kinesis-ingest"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "iot.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "iot_kinesis_ingest" {
  name = "kinesis-put-record"
  role = aws_iam_role.iot_kinesis_ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "PutSensorReading"
      Effect   = "Allow"
      Action   = ["kinesis:PutRecord"]
      Resource = aws_kinesis_stream.sensor_stream.arn
    }]
  })
}
