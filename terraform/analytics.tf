# ─────────────────────────────────────────────────────────
# Analytics — Glue Data Catalog + Athena + ETL Job
#
# Completa la pipeline de analytics sobre el data lake S3:
#   S3 (sensor-readings/) → Glue Catalog → Athena queries
#   Glue Python Shell Job (daily 03:00 UTC) → analytics-results/
#
# Referenciado desde kinesis.tf:11 — "Glue + Athena se configurarán
# en una fase posterior."
# ─────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────
# Glue Data Catalog — Database + Table
# ─────────────────────────────────────────────────────────

resource "aws_glue_catalog_database" "analytics" {
  name = "${local.name_prefix}-analytics"

  description = "SmartWaste sensor readings data lake — partitioned by year/month/day/hour"
}

# Tabla con partition projection: Athena descubre particiones automáticamente
# sin necesidad de un Glue Crawler (ahorra costo y complejidad).
resource "aws_glue_catalog_table" "sensor_readings" {
  database_name = aws_glue_catalog_database.analytics.name
  name          = "sensor_readings"
  description   = "Raw sensor readings from IoT containers — GZIP JSON, Hive-partitioned"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"                      = "json"
    "compressionType"                     = "gzip"
    # Partition projection — evita MSCK REPAIR TABLE o crawlers
    "projection.enabled"                  = "true"
    "projection.year.type"                = "integer"
    "projection.year.range"               = "2024,2030"
    "projection.month.type"               = "integer"
    "projection.month.range"              = "1,12"
    "projection.month.digits"             = "2"
    "projection.day.type"                 = "integer"
    "projection.day.range"                = "1,31"
    "projection.day.digits"               = "2"
    "projection.hour.type"                = "integer"
    "projection.hour.range"               = "0,23"
    "projection.hour.digits"              = "2"
    "storage.location.template"           = "s3://${aws_s3_bucket.data_lake.bucket}/sensor-readings/year=$${year}/month=$${month}/day=$${day}/hour=$${hour}/"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.bucket}/sensor-readings/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        "ignore.malformed.json" = "true"
        "case.insensitive"      = "true"
      }
    }

    columns {
      name = "container_id"
      type = "string"
    }
    columns {
      name = "timestamp"
      type = "string"
    }
    columns {
      name = "fill_level"
      type = "double"
    }
    columns {
      name = "battery"
      type = "double"
    }
    columns {
      name = "temperature"
      type = "double"
    }
    columns {
      name = "latitude"
      type = "double"
    }
    columns {
      name = "longitude"
      type = "double"
    }
  }

  partition_keys {
    name = "year"
    type = "int"
  }
  partition_keys {
    name = "month"
    type = "int"
  }
  partition_keys {
    name = "day"
    type = "int"
  }
  partition_keys {
    name = "hour"
    type = "int"
  }
}


# ─────────────────────────────────────────────────────────
# Athena Workgroup
# ─────────────────────────────────────────────────────────

resource "aws_athena_workgroup" "analytics" {
  name        = "${local.name_prefix}-analytics"
  description = "SmartWaste analytics queries — scan limit enforced for cost control"

  configuration {
    enforce_workgroup_configuration = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.data_lake.bucket}/athena-results/"
    }

    engine_version {
      selected_engine_version = "Athena engine version 3"
    }

    bytes_scanned_cutoff_per_query = 104857600 # 100 MB — cost guard for dev
  }

  tags = {
    Name = "${local.name_prefix}-analytics"
  }
}


# ─────────────────────────────────────────────────────────
# IAM — Glue ETL Job
# ─────────────────────────────────────────────────────────

resource "aws_iam_role" "glue_etl" {
  name = "${local.name_prefix}-glue-etl"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Managed policy for Glue service (CloudWatch logs, etc.)
resource "aws_iam_role_policy_attachment" "glue_etl_service" {
  role       = aws_iam_role.glue_etl.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue_etl_access" {
  name = "analytics-access"
  role = aws_iam_role.glue_etl.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ListBucket"
        Effect = "Allow"
        Action = ["s3:GetBucketLocation", "s3:ListBucket"]
        Resource = [aws_s3_bucket.data_lake.arn]
      },
      {
        # Spark/Hadoop escribe folder markers en el root del bucket
        # (ej: sensor-readings-parquet_$folder$) ademas de los objetos
        # dentro del prefijo. Necesitamos PutObject sobre todo el bucket.
        Sid    = "S3ReadWriteDataLake"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = ["${aws_s3_bucket.data_lake.arn}/*"]
      },
      {
        Sid    = "DynamoDBReadContainers"
        Effect = "Allow"
        Action = ["dynamodb:Scan"]
        Resource = aws_dynamodb_table.containers.arn
      },
      {
        Sid    = "DynamoDBReadRoutes"
        Effect = "Allow"
        Action = ["dynamodb:Scan"]
        Resource = aws_dynamodb_table.routes.arn
      },
    ]
  })
}


# ─────────────────────────────────────────────────────────
# Glue ETL Script — upload to S3
# ─────────────────────────────────────────────────────────

resource "aws_s3_object" "glue_etl_script" {
  bucket = aws_s3_bucket.data_lake.bucket
  key    = "glue-scripts/etl_daily.py"
  source = "${path.module}/../lambdas/glue-analytics/etl_daily.py"
  etag   = filemd5("${path.module}/../lambdas/glue-analytics/etl_daily.py")
}


# ─────────────────────────────────────────────────────────
# Glue ETL Job (PySpark, Glue 4.0)
#
# Reemplaza el Python Shell anterior. PySpark permite:
#   - Procesamiento paralelo de los 156K+ registros diarios
#   - Conversión Bronze (GZIP JSON) → Silver (Parquet) nativa
#   - groupBy/agg distribuidos para aggregaciones de ruta y sensor
#
# Costo: 2 workers × G.1X × ~45 min = ~$0.66/ejecución (~$20/mes)
# vs Python Shell: ~$0.03/ejecución pero sin paralelismo ni Parquet.
# ─────────────────────────────────────────────────────────

resource "aws_glue_job" "daily_analytics" {
  name         = "${local.name_prefix}-daily-analytics"
  role_arn     = aws_iam_role.glue_etl.arn
  glue_version = "4.0"

  command {
    name            = "glueetl"
    python_version  = "3"
    script_location = "s3://${aws_s3_object.glue_etl_script.bucket}/${aws_s3_object.glue_etl_script.key}"
  }

  # G.1X: 4 vCPU / 16 GB por worker — suficiente para 156K lecturas/día
  # 2 workers: 1 driver + 1 executor (mínimo para glueetl)
  number_of_workers = 2
  worker_type       = "G.1X"

  default_arguments = {
    # scipy se instala en cada worker; numpy viene pre-instalado en Glue 4.0
    "--additional-python-modules"        = "scipy"
    "--enable-metrics"                   = ""
    "--enable-continuous-cloudwatch-log" = "true"
    "--DATA_LAKE_BUCKET"                 = aws_s3_bucket.data_lake.bucket
    "--CONTAINERS_TABLE"                 = aws_dynamodb_table.containers.name
    "--ROUTES_TABLE"                     = aws_dynamodb_table.routes.name
  }

  timeout     = 60 # minutos — Spark startup tarda ~3 min + processing
  max_retries = 0

  tags = {
    Name = "${local.name_prefix}-daily-analytics"
  }
}


# ─────────────────────────────────────────────────────────
# Glue Trigger — daily at 03:00 UTC (medianoche MVD)
# ─────────────────────────────────────────────────────────

resource "aws_glue_trigger" "daily_analytics" {
  name     = "${local.name_prefix}-daily-analytics"
  type     = "SCHEDULED"
  schedule = "cron(0 3 * * ? *)"

  actions {
    job_name = aws_glue_job.daily_analytics.name
  }

  tags = {
    Name = "${local.name_prefix}-daily-analytics"
  }
}


# ─────────────────────────────────────────────────────────
# Glue Catalog Tables — Silver + Bronze (para Athena ad-hoc)
# ─────────────────────────────────────────────────────────

# Silver layer: Parquet sensor readings, partitioned by date.
# Escrita diariamente por el Glue ETL. Mucho más eficiente que
# el Bronze GZIP para queries Athena (~10-100x menos datos escaneados).
resource "aws_glue_catalog_table" "sensor_readings_parquet" {
  database_name = aws_glue_catalog_database.analytics.name
  name          = "sensor_readings_parquet"
  description   = "Sensor readings Silver layer — Parquet columnar, partitioned by date"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"                = "parquet"
    "projection.enabled"            = "true"
    "projection.date.type"          = "date"
    "projection.date.range"         = "2024-01-01,NOW"
    "projection.date.format"        = "yyyy-MM-dd"
    "projection.date.interval"      = "1"
    "projection.date.interval.unit" = "DAYS"
    "storage.location.template"     = "s3://${aws_s3_bucket.data_lake.bucket}/sensor-readings-parquet/date=$${date}/"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.bucket}/sensor-readings-parquet/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    columns {
      name = "container_id"
      type = "string"
    }
    columns {
      name = "timestamp"
      type = "string"
    }
    columns {
      name = "fill_level"
      type = "double"
    }
    columns {
      name = "battery"
      type = "double"
    }
    columns {
      name = "temperature"
      type = "double"
    }
    columns {
      name = "latitude"
      type = "double"
    }
    columns {
      name = "longitude"
      type = "double"
    }
  }

  partition_keys {
    name = "date"
    type = "string"
  }
}

# Bronze layer: Route optimization results escritos por Kinesis Firehose.
# Firehose usa el mismo formato Hive (year/month/day/hour) que sensor-readings.
# Múltiples registros por archivo GZIP (Firehose los bufferiza 5MB/5min).
resource "aws_glue_catalog_table" "route_results" {
  database_name = aws_glue_catalog_database.analytics.name
  name          = "route_results"
  description   = "Route optimization results Bronze — GZIP NDJSON from Firehose, partitioned by year/month/day/hour"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"                      = "json"
    "compressionType"                     = "gzip"
    "projection.enabled"                  = "true"
    "projection.year.type"                = "integer"
    "projection.year.range"               = "2024,2030"
    "projection.month.type"               = "integer"
    "projection.month.range"              = "1,12"
    "projection.month.digits"             = "2"
    "projection.day.type"                 = "integer"
    "projection.day.range"                = "1,31"
    "projection.day.digits"               = "2"
    "projection.hour.type"                = "integer"
    "projection.hour.range"               = "0,23"
    "projection.hour.digits"              = "2"
    "storage.location.template"           = "s3://${aws_s3_bucket.data_lake.bucket}/route-results/year=$${year}/month=$${month}/day=$${day}/hour=$${hour}/"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.bucket}/route-results/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        "ignore.malformed.json" = "true"
        "case.insensitive"      = "true"
      }
    }

    columns {
      name = "route_id"
      type = "string"
    }
    columns {
      name = "circuit_id"
      type = "string"
    }
    columns {
      name = "truck_id"
      type = "string"
    }
    columns {
      name = "date"
      type = "string"
    }
    columns {
      name = "created_at"
      type = "string"
    }
    columns {
      name = "baseline_distance_m"
      type = "bigint"
    }
    columns {
      name = "total_distance_m"
      type = "bigint"
    }
    columns {
      name = "baseline_duration_s"
      type = "bigint"
    }
    columns {
      name = "total_duration_s"
      type = "bigint"
    }
    columns {
      name = "baseline_stops"
      type = "int"
    }
    columns {
      name = "optimized_stops"
      type = "int"
    }
    columns {
      name = "stops_skipped"
      type = "int"
    }
    columns {
      name = "distance_improvement_pct"
      type = "double"
    }
    columns {
      name = "duration_improvement_pct"
      type = "double"
    }
    columns {
      name = "solver"
      type = "string"
    }
    columns {
      name = "solver_status"
      type = "string"
    }
  }

  partition_keys {
    name = "year"
    type = "int"
  }
  partition_keys {
    name = "month"
    type = "int"
  }
  partition_keys {
    name = "day"
    type = "int"
  }
  partition_keys {
    name = "hour"
    type = "int"
  }
}

# Silver layer: Route results convertidos a Parquet por el Glue ETL.
# Mucho más eficiente que el Bronze GZIP para queries Athena ad-hoc.
resource "aws_glue_catalog_table" "route_results_parquet" {
  database_name = aws_glue_catalog_database.analytics.name
  name          = "route_results_parquet"
  description   = "Route results Silver layer — Parquet columnar, partitioned by date"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"                = "parquet"
    "projection.enabled"            = "true"
    "projection.date.type"          = "date"
    "projection.date.range"         = "2024-01-01,NOW"
    "projection.date.format"        = "yyyy-MM-dd"
    "projection.date.interval"      = "1"
    "projection.date.interval.unit" = "DAYS"
    "storage.location.template"     = "s3://${aws_s3_bucket.data_lake.bucket}/route-results-parquet/date=$${date}/"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.bucket}/route-results-parquet/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters            = { "serialization.format" = "1" }
    }

    columns {
      name = "route_id"
      type = "string"
    }
    columns {
      name = "circuit_id"
      type = "string"
    }
    columns {
      name = "truck_id"
      type = "string"
    }
    columns {
      name = "created_at"
      type = "string"
    }
    columns {
      name = "baseline_distance_m"
      type = "bigint"
    }
    columns {
      name = "total_distance_m"
      type = "bigint"
    }
    columns {
      name = "baseline_duration_s"
      type = "bigint"
    }
    columns {
      name = "total_duration_s"
      type = "bigint"
    }
    columns {
      name = "baseline_stops"
      type = "int"
    }
    columns {
      name = "optimized_stops"
      type = "int"
    }
    columns {
      name = "stops_skipped"
      type = "int"
    }
    columns {
      name = "distance_improvement_pct"
      type = "double"
    }
    columns {
      name = "duration_improvement_pct"
      type = "double"
    }
    columns {
      name = "solver"
      type = "string"
    }
    columns {
      name = "solver_status"
      type = "string"
    }
  }

  partition_keys {
    name = "date"
    type = "string"
  }
}
