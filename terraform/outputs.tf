# ─────────────────────────────────────────────────────────
# Outputs — SmartWaste MVD
#
# Los valores de estos outputs se usan directamente en las
# variables de entorno de las Lambdas, el simulador y el
# script de seed de DynamoDB.
#
# Para ver todos los valores tras un apply:
#   terraform output
#   terraform output -json   # formato JSON para scripts
# ─────────────────────────────────────────────────────────

# ── DynamoDB ──────────────────────────────────────────────

output "dynamodb_containers_table_name" {
  description = "Nombre de la tabla DynamoDB de contenedores (DYNAMODB_CONTAINERS_TABLE)"
  value       = aws_dynamodb_table.containers.name
}

output "dynamodb_containers_table_arn" {
  description = "ARN de la tabla DynamoDB de contenedores"
  value       = aws_dynamodb_table.containers.arn
}

output "dynamodb_trucks_table_name" {
  description = "Nombre de la tabla DynamoDB de camiones (DYNAMODB_TRUCKS_TABLE)"
  value       = aws_dynamodb_table.trucks.name
}

output "dynamodb_trucks_table_arn" {
  description = "ARN de la tabla DynamoDB de camiones"
  value       = aws_dynamodb_table.trucks.arn
}

output "dynamodb_routes_table_name" {
  description = "Nombre de la tabla DynamoDB de rutas (DYNAMODB_ROUTES_TABLE)"
  value       = aws_dynamodb_table.routes.name
}

output "dynamodb_routes_table_arn" {
  description = "ARN de la tabla DynamoDB de rutas"
  value       = aws_dynamodb_table.routes.arn
}

output "dynamodb_sensor_readings_table_name" {
  description = "Nombre de la tabla DynamoDB de lecturas de sensores"
  value       = aws_dynamodb_table.sensor_readings.name
}

output "dynamodb_sensor_readings_table_arn" {
  description = "ARN de la tabla DynamoDB de lecturas de sensores"
  value       = aws_dynamodb_table.sensor_readings.arn
}

# ── IoT Core ──────────────────────────────────────────────

output "iot_endpoint" {
  description = "Endpoint ATS de AWS IoT Core para esta cuenta/región (IOT_ENDPOINT)"
  value       = data.aws_iot_endpoint.ats.endpoint_address
}

output "iot_thing_type_name" {
  description = "Nombre del IoT Thing Type para contenedores"
  value       = aws_iot_thing_type.waste_container.name
}

output "iot_sensor_policy_arn" {
  description = "ARN de la política IoT asignada a los dispositivos sensor"
  value       = aws_iot_policy.sensor_policy.arn
}

output "iot_sensor_policy_name" {
  description = "Nombre de la política IoT asignada a los dispositivos sensor"
  value       = aws_iot_policy.sensor_policy.name
}

# ── Contexto de despliegue ────────────────────────────────

output "aws_account_id" {
  description = "ID de la cuenta AWS donde se desplegó la infraestructura"
  value       = local.account_id
}

output "aws_region" {
  description = "Región AWS del despliegue"
  value       = local.region
}

output "environment" {
  description = "Nombre del entorno desplegado"
  value       = var.environment
}

output "name_prefix" {
  description = "Prefijo efectivo usado en todos los recursos de este entorno"
  value       = local.name_prefix
}

# ── Data Lake ─────────────────────────────────────────────

output "data_lake_bucket_name" {
  description = "Nombre del bucket S3 del Data Lake"
  value       = aws_s3_bucket.data_lake.bucket
}

output "data_lake_bucket_arn" {
  description = "ARN del bucket S3 del Data Lake"
  value       = aws_s3_bucket.data_lake.arn
}

output "kinesis_stream_name" {
  description = "Nombre del Kinesis Data Stream de lecturas de sensores"
  value       = aws_kinesis_stream.sensor_stream.name
}

output "kinesis_stream_arn" {
  description = "ARN del Kinesis Data Stream de lecturas de sensores"
  value       = aws_kinesis_stream.sensor_stream.arn
}

output "firehose_stream_name" {
  description = "Nombre del Kinesis Firehose delivery stream"
  value       = aws_kinesis_firehose_delivery_stream.sensor_firehose.name
}

output "firehose_stream_arn" {
  description = "ARN del Kinesis Firehose delivery stream"
  value       = aws_kinesis_firehose_delivery_stream.sensor_firehose.arn
}

# ── Bloque de variables de entorno ───────────────────────
# Copia este bloque en tu .env o en la configuración de las
# Lambdas y el simulador.

# ── API Gateway ───────────────────────────────────────────

# ── WebSocket API ──────────────────────────────────────────

output "websocket_url" {
  description = "URL WebSocket para el conductor (conectar con wscat o la driver app)"
  value       = aws_apigatewayv2_stage.ws_dev.invoke_url
}

output "websocket_endpoint" {
  description = "URL HTTPS de la Management API (WS_ENDPOINT para ws_notifier)"
  value       = "https://${aws_apigatewayv2_api.smartwaste_ws.id}.execute-api.${local.region}.amazonaws.com/dev"
}

output "connections_table_name" {
  description = "Nombre de la tabla DynamoDB de conexiones WebSocket"
  value       = aws_dynamodb_table.connections.name
}

output "api_url" {
  description = "URL base de la REST API (stage dev)"
  value       = "https://${aws_api_gateway_rest_api.smartwaste.id}.execute-api.${local.region}.amazonaws.com/dev"
}

output "api_id" {
  description = "ID de la REST API Gateway"
  value       = aws_api_gateway_rest_api.smartwaste.id
}

# ── VPC ───────────────────────────────────────────────────

output "vpc_id" {
  description = "ID de la VPC privada"
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "IDs de las subnets privadas (Lambda + ECS)"
  value       = aws_subnet.private[*].id
}

# ── OSRM / ECS ────────────────────────────────────────────

output "osrm_ecr_repository_url" {
  description = "URL del repositorio ECR con la imagen OSRM"
  value       = aws_ecr_repository.osrm.repository_url
}

output "osrm_ecs_cluster" {
  description = "Nombre del cluster ECS"
  value       = aws_ecs_cluster.main.name
}

output "osrm_service_url" {
  description = "URL interna del servidor OSRM (Cloud Map DNS, solo accesible desde la VPC)"
  value       = "http://osrm.smartwaste.local:5000"
}

output "env_block" {
  description = "Variables de entorno listas para copiar en .env o Lambda config"
  value       = <<-EOT
    AWS_REGION=${local.region}
    DYNAMODB_CONTAINERS_TABLE=${aws_dynamodb_table.containers.name}
    DYNAMODB_TRUCKS_TABLE=${aws_dynamodb_table.trucks.name}
    DYNAMODB_ROUTES_TABLE=${aws_dynamodb_table.routes.name}
    DYNAMODB_SENSOR_READINGS_TABLE=${aws_dynamodb_table.sensor_readings.name}
    IOT_ENDPOINT=${data.aws_iot_endpoint.ats.endpoint_address}
    KINESIS_STREAM_NAME=${aws_kinesis_stream.sensor_stream.name}
    DATA_LAKE_BUCKET=${aws_s3_bucket.data_lake.bucket}
    API_URL=https://${aws_api_gateway_rest_api.smartwaste.id}.execute-api.${local.region}.amazonaws.com/dev
    WEBSOCKET_URL=${aws_apigatewayv2_stage.ws_dev.invoke_url}
    WS_ENDPOINT=https://${aws_apigatewayv2_api.smartwaste_ws.id}.execute-api.${local.region}.amazonaws.com/dev
    CONNECTIONS_TABLE=${aws_dynamodb_table.connections.name}
  EOT
}
