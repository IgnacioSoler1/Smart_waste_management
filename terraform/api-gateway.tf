# ─────────────────────────────────────────────────────────
# API Gateway REST — SmartWaste MVD
#
# Endpoints:
#   GET  /circuits                   → lista circuitos con stats
#   GET  /circuits/{id}/containers   → contenedores del circuito
#   GET  /circuits/{id}/route        → ruta activa del circuito
#   GET  /trucks                     → lista camiones con estado
#   POST /optimize/{circuit_id}      → trigger manual de optimización
#
# Todas las rutas usan Lambda proxy integration (AWS_PROXY).
# CORS: OPTIONS + MOCK integration en cada recurso.
# ─────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────
# Lambda — api handler
# ─────────────────────────────────────────────────────────

# Sin dependencias externas: boto3 viene incluido en Python 3.11 de Lambda.
# Se empaqueta directo — no necesita build.sh ni upload a S3.
data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/api"
  output_path = "${path.module}/.terraform/api.zip"
}

# ── IAM ──────────────────────────────────────────────────

resource "aws_iam_role" "api" {
  name = "${local.name_prefix}-api"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "api_logs" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Lectura de las tres tablas operativas y sus GSIs
resource "aws_iam_role_policy" "api_dynamo" {
  name = "dynamodb-read"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ReadOperationalTables"
      Effect = "Allow"
      Action = ["dynamodb:Scan", "dynamodb:Query", "dynamodb:GetItem"]
      Resource = [
        aws_dynamodb_table.containers.arn,
        "${aws_dynamodb_table.containers.arn}/index/circuit-index",
        aws_dynamodb_table.trucks.arn,
        "${aws_dynamodb_table.trucks.arn}/index/status-index",
        aws_dynamodb_table.routes.arn,
        "${aws_dynamodb_table.routes.arn}/index/circuit-index",
      ]
    }]
  })
}

# Invocación async de route-optimizer para el endpoint POST /optimize/{id}
resource "aws_iam_role_policy" "api_lambda_invoke" {
  name = "invoke-route-optimizer"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeRouteOptimizer"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.route_optimizer.arn
    }]
  })
}

# S3 read access for analytics results (latest.json, trends)
resource "aws_iam_role_policy" "api_s3_analytics" {
  name = "s3-read-analytics"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadAnalyticsResults"
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "${aws_s3_bucket.data_lake.arn}/analytics-results/*"
    }]
  })
}

# ── Lambda function ───────────────────────────────────────

resource "aws_lambda_function" "api" {
  function_name    = "${local.name_prefix}-api"
  description      = "REST API: circuitos, camiones, rutas y trigger de optimización"
  role             = aws_iam_role.api.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  # 256 MB: las respuestas incluyen listas de hasta ~100 contenedores.
  # 15 s: el Scan de rutas para /circuits/{id}/route puede tardar < 1 s;
  #       15 s cubre el cold start y picos de latencia de DynamoDB.
  memory_size = 256
  timeout     = 15

  environment {
    variables = {
      CONTAINERS_TABLE         = aws_dynamodb_table.containers.name
      TRUCKS_TABLE             = aws_dynamodb_table.trucks.name
      ROUTES_TABLE             = aws_dynamodb_table.routes.name
      ROUTE_OPTIMIZER_FUNCTION = aws_lambda_function.route_optimizer.function_name
      DATA_LAKE_BUCKET         = aws_s3_bucket.data_lake.bucket
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.api_logs,
    aws_iam_role_policy.api_dynamo,
    aws_iam_role_policy.api_lambda_invoke,
    aws_iam_role_policy.api_s3_analytics,
  ]
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${aws_lambda_function.api.function_name}"
  retention_in_days = 14
}


# ─────────────────────────────────────────────────────────
# REST API
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_rest_api" "smartwaste" {
  name        = "${local.name_prefix}-api"
  description = "SmartWaste MVD REST API — circuitos, camiones, rutas y optimización"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# ─────────────────────────────────────────────────────────
# Resources (árbol de paths)
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_resource" "circuits" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_rest_api.smartwaste.root_resource_id
  path_part   = "circuits"
}

# /circuits/{id}  — recurso intermedio; no tiene métodos propios
resource "aws_api_gateway_resource" "circuit" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_resource.circuits.id
  path_part   = "{id}"
}

resource "aws_api_gateway_resource" "circuit_containers" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_resource.circuit.id
  path_part   = "containers"
}

resource "aws_api_gateway_resource" "circuit_route" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_resource.circuit.id
  path_part   = "route"
}

resource "aws_api_gateway_resource" "trucks" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_rest_api.smartwaste.root_resource_id
  path_part   = "trucks"
}

resource "aws_api_gateway_resource" "routes" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_rest_api.smartwaste.root_resource_id
  path_part   = "routes"
}

resource "aws_api_gateway_resource" "routes_comparison" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_resource.routes.id
  path_part   = "comparison"
}

resource "aws_api_gateway_resource" "optimize" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_rest_api.smartwaste.root_resource_id
  path_part   = "optimize"
}

resource "aws_api_gateway_resource" "optimize_circuit" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_resource.optimize.id
  path_part   = "{circuit_id}"
}


# ─────────────────────────────────────────────────────────
# Locals: valores CORS comunes
# ─────────────────────────────────────────────────────────

locals {
  # Valores de las cabeceras CORS que se devuelven en el OPTIONS mock.
  # Las comillas simples internas son requeridas por la sintaxis de
  # API Gateway para valores literales en integration_response.
  cors_headers = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,Authorization,X-Api-Key,X-Amz-Date'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }
}


# ─────────────────────────────────────────────────────────
# GET /circuits
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_method" "circuits_get" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.circuits.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "circuits_get" {
  rest_api_id             = aws_api_gateway_rest_api.smartwaste.id
  resource_id             = aws_api_gateway_resource.circuits.id
  http_method             = aws_api_gateway_method.circuits_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# OPTIONS /circuits — CORS preflight
resource "aws_api_gateway_method" "circuits_options" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.circuits.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "circuits_options" {
  rest_api_id          = aws_api_gateway_rest_api.smartwaste.id
  resource_id          = aws_api_gateway_resource.circuits.id
  http_method          = aws_api_gateway_method.circuits_options.http_method
  type                 = "MOCK"
  request_templates    = { "application/json" = "{\"statusCode\": 200}" }
  passthrough_behavior = "WHEN_NO_MATCH"
}

resource "aws_api_gateway_method_response" "circuits_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.circuits.id
  http_method = aws_api_gateway_method.circuits_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = { "application/json" = "Empty" }
}

resource "aws_api_gateway_integration_response" "circuits_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.circuits.id
  http_method = aws_api_gateway_method.circuits_options.http_method
  status_code = aws_api_gateway_method_response.circuits_options.status_code

  response_parameters = local.cors_headers

  depends_on = [aws_api_gateway_integration.circuits_options]
}


# ─────────────────────────────────────────────────────────
# GET /circuits/{id}/containers
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_method" "circuit_containers_get" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.circuit_containers.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "circuit_containers_get" {
  rest_api_id             = aws_api_gateway_rest_api.smartwaste.id
  resource_id             = aws_api_gateway_resource.circuit_containers.id
  http_method             = aws_api_gateway_method.circuit_containers_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# OPTIONS /circuits/{id}/containers
resource "aws_api_gateway_method" "circuit_containers_options" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.circuit_containers.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "circuit_containers_options" {
  rest_api_id          = aws_api_gateway_rest_api.smartwaste.id
  resource_id          = aws_api_gateway_resource.circuit_containers.id
  http_method          = aws_api_gateway_method.circuit_containers_options.http_method
  type                 = "MOCK"
  request_templates    = { "application/json" = "{\"statusCode\": 200}" }
  passthrough_behavior = "WHEN_NO_MATCH"
}

resource "aws_api_gateway_method_response" "circuit_containers_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.circuit_containers.id
  http_method = aws_api_gateway_method.circuit_containers_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = { "application/json" = "Empty" }
}

resource "aws_api_gateway_integration_response" "circuit_containers_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.circuit_containers.id
  http_method = aws_api_gateway_method.circuit_containers_options.http_method
  status_code = aws_api_gateway_method_response.circuit_containers_options.status_code

  response_parameters = local.cors_headers

  depends_on = [aws_api_gateway_integration.circuit_containers_options]
}


# ─────────────────────────────────────────────────────────
# GET /circuits/{id}/route
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_method" "circuit_route_get" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.circuit_route.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "circuit_route_get" {
  rest_api_id             = aws_api_gateway_rest_api.smartwaste.id
  resource_id             = aws_api_gateway_resource.circuit_route.id
  http_method             = aws_api_gateway_method.circuit_route_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# OPTIONS /circuits/{id}/route
resource "aws_api_gateway_method" "circuit_route_options" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.circuit_route.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "circuit_route_options" {
  rest_api_id          = aws_api_gateway_rest_api.smartwaste.id
  resource_id          = aws_api_gateway_resource.circuit_route.id
  http_method          = aws_api_gateway_method.circuit_route_options.http_method
  type                 = "MOCK"
  request_templates    = { "application/json" = "{\"statusCode\": 200}" }
  passthrough_behavior = "WHEN_NO_MATCH"
}

resource "aws_api_gateway_method_response" "circuit_route_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.circuit_route.id
  http_method = aws_api_gateway_method.circuit_route_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = { "application/json" = "Empty" }
}

resource "aws_api_gateway_integration_response" "circuit_route_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.circuit_route.id
  http_method = aws_api_gateway_method.circuit_route_options.http_method
  status_code = aws_api_gateway_method_response.circuit_route_options.status_code

  response_parameters = local.cors_headers

  depends_on = [aws_api_gateway_integration.circuit_route_options]
}


# ─────────────────────────────────────────────────────────
# GET /trucks
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_method" "trucks_get" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.trucks.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "trucks_get" {
  rest_api_id             = aws_api_gateway_rest_api.smartwaste.id
  resource_id             = aws_api_gateway_resource.trucks.id
  http_method             = aws_api_gateway_method.trucks_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# OPTIONS /trucks
resource "aws_api_gateway_method" "trucks_options" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.trucks.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "trucks_options" {
  rest_api_id          = aws_api_gateway_rest_api.smartwaste.id
  resource_id          = aws_api_gateway_resource.trucks.id
  http_method          = aws_api_gateway_method.trucks_options.http_method
  type                 = "MOCK"
  request_templates    = { "application/json" = "{\"statusCode\": 200}" }
  passthrough_behavior = "WHEN_NO_MATCH"
}

resource "aws_api_gateway_method_response" "trucks_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.trucks.id
  http_method = aws_api_gateway_method.trucks_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = { "application/json" = "Empty" }
}

resource "aws_api_gateway_integration_response" "trucks_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.trucks.id
  http_method = aws_api_gateway_method.trucks_options.http_method
  status_code = aws_api_gateway_method_response.trucks_options.status_code

  response_parameters = local.cors_headers

  depends_on = [aws_api_gateway_integration.trucks_options]
}


# ─────────────────────────────────────────────────────────
# GET /routes/comparison
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_method" "routes_comparison_get" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.routes_comparison.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "routes_comparison_get" {
  rest_api_id             = aws_api_gateway_rest_api.smartwaste.id
  resource_id             = aws_api_gateway_resource.routes_comparison.id
  http_method             = aws_api_gateway_method.routes_comparison_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# OPTIONS /routes/comparison — CORS preflight
resource "aws_api_gateway_method" "routes_comparison_options" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.routes_comparison.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "routes_comparison_options" {
  rest_api_id          = aws_api_gateway_rest_api.smartwaste.id
  resource_id          = aws_api_gateway_resource.routes_comparison.id
  http_method          = aws_api_gateway_method.routes_comparison_options.http_method
  type                 = "MOCK"
  request_templates    = { "application/json" = "{\"statusCode\": 200}" }
  passthrough_behavior = "WHEN_NO_MATCH"
}

resource "aws_api_gateway_method_response" "routes_comparison_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.routes_comparison.id
  http_method = aws_api_gateway_method.routes_comparison_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = { "application/json" = "Empty" }
}

resource "aws_api_gateway_integration_response" "routes_comparison_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.routes_comparison.id
  http_method = aws_api_gateway_method.routes_comparison_options.http_method
  status_code = aws_api_gateway_method_response.routes_comparison_options.status_code

  response_parameters = local.cors_headers

  depends_on = [aws_api_gateway_integration.routes_comparison_options]
}


# ─────────────────────────────────────────────────────────
# POST /optimize/{circuit_id}
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_method" "optimize_circuit_post" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.optimize_circuit.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "optimize_circuit_post" {
  rest_api_id             = aws_api_gateway_rest_api.smartwaste.id
  resource_id             = aws_api_gateway_resource.optimize_circuit.id
  http_method             = aws_api_gateway_method.optimize_circuit_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# OPTIONS /optimize/{circuit_id}
resource "aws_api_gateway_method" "optimize_circuit_options" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.optimize_circuit.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "optimize_circuit_options" {
  rest_api_id          = aws_api_gateway_rest_api.smartwaste.id
  resource_id          = aws_api_gateway_resource.optimize_circuit.id
  http_method          = aws_api_gateway_method.optimize_circuit_options.http_method
  type                 = "MOCK"
  request_templates    = { "application/json" = "{\"statusCode\": 200}" }
  passthrough_behavior = "WHEN_NO_MATCH"
}

resource "aws_api_gateway_method_response" "optimize_circuit_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.optimize_circuit.id
  http_method = aws_api_gateway_method.optimize_circuit_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = { "application/json" = "Empty" }
}

resource "aws_api_gateway_integration_response" "optimize_circuit_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.optimize_circuit.id
  http_method = aws_api_gateway_method.optimize_circuit_options.http_method
  status_code = aws_api_gateway_method_response.optimize_circuit_options.status_code

  response_parameters = local.cors_headers

  depends_on = [aws_api_gateway_integration.optimize_circuit_options]
}


# ─────────────────────────────────────────────────────────
# GET /analytics/summary
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_resource" "analytics" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_rest_api.smartwaste.root_resource_id
  path_part   = "analytics"
}

resource "aws_api_gateway_resource" "analytics_summary" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_resource.analytics.id
  path_part   = "summary"
}

resource "aws_api_gateway_method" "analytics_summary_get" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.analytics_summary.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "analytics_summary_get" {
  rest_api_id             = aws_api_gateway_rest_api.smartwaste.id
  resource_id             = aws_api_gateway_resource.analytics_summary.id
  http_method             = aws_api_gateway_method.analytics_summary_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# OPTIONS /analytics/summary
resource "aws_api_gateway_method" "analytics_summary_options" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.analytics_summary.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "analytics_summary_options" {
  rest_api_id          = aws_api_gateway_rest_api.smartwaste.id
  resource_id          = aws_api_gateway_resource.analytics_summary.id
  http_method          = aws_api_gateway_method.analytics_summary_options.http_method
  type                 = "MOCK"
  request_templates    = { "application/json" = "{\"statusCode\": 200}" }
  passthrough_behavior = "WHEN_NO_MATCH"
}

resource "aws_api_gateway_method_response" "analytics_summary_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.analytics_summary.id
  http_method = aws_api_gateway_method.analytics_summary_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = { "application/json" = "Empty" }
}

resource "aws_api_gateway_integration_response" "analytics_summary_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.analytics_summary.id
  http_method = aws_api_gateway_method.analytics_summary_options.http_method
  status_code = aws_api_gateway_method_response.analytics_summary_options.status_code

  response_parameters = local.cors_headers

  depends_on = [aws_api_gateway_integration.analytics_summary_options]
}


# ─────────────────────────────────────────────────────────
# GET /analytics/trends
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_resource" "analytics_trends" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_resource.analytics.id
  path_part   = "trends"
}

resource "aws_api_gateway_method" "analytics_trends_get" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.analytics_trends.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "analytics_trends_get" {
  rest_api_id             = aws_api_gateway_rest_api.smartwaste.id
  resource_id             = aws_api_gateway_resource.analytics_trends.id
  http_method             = aws_api_gateway_method.analytics_trends_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# OPTIONS /analytics/trends
resource "aws_api_gateway_method" "analytics_trends_options" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.analytics_trends.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "analytics_trends_options" {
  rest_api_id          = aws_api_gateway_rest_api.smartwaste.id
  resource_id          = aws_api_gateway_resource.analytics_trends.id
  http_method          = aws_api_gateway_method.analytics_trends_options.http_method
  type                 = "MOCK"
  request_templates    = { "application/json" = "{\"statusCode\": 200}" }
  passthrough_behavior = "WHEN_NO_MATCH"
}

resource "aws_api_gateway_method_response" "analytics_trends_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.analytics_trends.id
  http_method = aws_api_gateway_method.analytics_trends_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = { "application/json" = "Empty" }
}

resource "aws_api_gateway_integration_response" "analytics_trends_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.analytics_trends.id
  http_method = aws_api_gateway_method.analytics_trends_options.http_method
  status_code = aws_api_gateway_method_response.analytics_trends_options.status_code

  response_parameters = local.cors_headers

  depends_on = [aws_api_gateway_integration.analytics_trends_options]
}


# ─────────────────────────────────────────────────────────
# GET /analytics/route-efficiency-trends
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_resource" "analytics_route_efficiency_trends" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  parent_id   = aws_api_gateway_resource.analytics.id
  path_part   = "route-efficiency-trends"
}

resource "aws_api_gateway_method" "analytics_route_efficiency_trends_get" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.analytics_route_efficiency_trends.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "analytics_route_efficiency_trends_get" {
  rest_api_id             = aws_api_gateway_rest_api.smartwaste.id
  resource_id             = aws_api_gateway_resource.analytics_route_efficiency_trends.id
  http_method             = aws_api_gateway_method.analytics_route_efficiency_trends_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# OPTIONS /analytics/route-efficiency-trends
resource "aws_api_gateway_method" "analytics_route_efficiency_trends_options" {
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  resource_id   = aws_api_gateway_resource.analytics_route_efficiency_trends.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "analytics_route_efficiency_trends_options" {
  rest_api_id          = aws_api_gateway_rest_api.smartwaste.id
  resource_id          = aws_api_gateway_resource.analytics_route_efficiency_trends.id
  http_method          = aws_api_gateway_method.analytics_route_efficiency_trends_options.http_method
  type                 = "MOCK"
  request_templates    = { "application/json" = "{\"statusCode\": 200}" }
  passthrough_behavior = "WHEN_NO_MATCH"
}

resource "aws_api_gateway_method_response" "analytics_route_efficiency_trends_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.analytics_route_efficiency_trends.id
  http_method = aws_api_gateway_method.analytics_route_efficiency_trends_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = { "application/json" = "Empty" }
}

resource "aws_api_gateway_integration_response" "analytics_route_efficiency_trends_options" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id
  resource_id = aws_api_gateway_resource.analytics_route_efficiency_trends.id
  http_method = aws_api_gateway_method.analytics_route_efficiency_trends_options.http_method
  status_code = aws_api_gateway_method_response.analytics_route_efficiency_trends_options.status_code

  response_parameters = local.cors_headers

  depends_on = [aws_api_gateway_integration.analytics_route_efficiency_trends_options]
}


# ─────────────────────────────────────────────────────────
# Deployment y Stage
# ─────────────────────────────────────────────────────────

resource "aws_api_gateway_deployment" "smartwaste" {
  rest_api_id = aws_api_gateway_rest_api.smartwaste.id

  # Fuerza redeploy automático cuando cambia cualquier método o integración.
  # Sin este trigger, Terraform no redeployaría al actualizar métodos
  # (el deployment es un recurso independiente de los métodos en AWS).
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.circuits.id,
      aws_api_gateway_resource.circuit.id,
      aws_api_gateway_resource.circuit_containers.id,
      aws_api_gateway_resource.circuit_route.id,
      aws_api_gateway_resource.trucks.id,
      aws_api_gateway_resource.routes.id,
      aws_api_gateway_resource.routes_comparison.id,
      aws_api_gateway_resource.optimize.id,
      aws_api_gateway_resource.optimize_circuit.id,
      aws_api_gateway_resource.analytics.id,
      aws_api_gateway_resource.analytics_summary.id,
      aws_api_gateway_resource.analytics_trends.id,
      aws_api_gateway_resource.analytics_route_efficiency_trends.id,

      aws_api_gateway_integration.circuits_get.id,
      aws_api_gateway_integration.circuits_options.id,
      aws_api_gateway_integration.circuit_containers_get.id,
      aws_api_gateway_integration.circuit_containers_options.id,
      aws_api_gateway_integration.circuit_route_get.id,
      aws_api_gateway_integration.circuit_route_options.id,
      aws_api_gateway_integration.trucks_get.id,
      aws_api_gateway_integration.trucks_options.id,
      aws_api_gateway_integration.routes_comparison_get.id,
      aws_api_gateway_integration.routes_comparison_options.id,
      aws_api_gateway_integration.optimize_circuit_post.id,
      aws_api_gateway_integration.optimize_circuit_options.id,
      aws_api_gateway_integration.analytics_summary_get.id,
      aws_api_gateway_integration.analytics_summary_options.id,
      aws_api_gateway_integration.analytics_trends_get.id,
      aws_api_gateway_integration.analytics_trends_options.id,
      aws_api_gateway_integration.analytics_route_efficiency_trends_get.id,
      aws_api_gateway_integration.analytics_route_efficiency_trends_options.id,
    ]))
  }

  # create_before_destroy evita downtime al redesplegar:
  # primero crea el nuevo deployment, luego destruye el anterior.
  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    # Métodos GET/POST
    aws_api_gateway_method.circuits_get,
    aws_api_gateway_method.circuit_containers_get,
    aws_api_gateway_method.circuit_route_get,
    aws_api_gateway_method.trucks_get,
    aws_api_gateway_method.routes_comparison_get,
    aws_api_gateway_method.optimize_circuit_post,
    aws_api_gateway_method.analytics_summary_get,
    aws_api_gateway_method.analytics_trends_get,
    aws_api_gateway_method.analytics_route_efficiency_trends_get,
    # Integraciones GET/POST
    aws_api_gateway_integration.circuits_get,
    aws_api_gateway_integration.circuit_containers_get,
    aws_api_gateway_integration.circuit_route_get,
    aws_api_gateway_integration.trucks_get,
    aws_api_gateway_integration.routes_comparison_get,
    aws_api_gateway_integration.optimize_circuit_post,
    aws_api_gateway_integration.analytics_summary_get,
    aws_api_gateway_integration.analytics_trends_get,
    aws_api_gateway_integration.analytics_route_efficiency_trends_get,
    # Métodos OPTIONS
    aws_api_gateway_method.circuits_options,
    aws_api_gateway_method.circuit_containers_options,
    aws_api_gateway_method.circuit_route_options,
    aws_api_gateway_method.trucks_options,
    aws_api_gateway_method.routes_comparison_options,
    aws_api_gateway_method.optimize_circuit_options,
    aws_api_gateway_method.analytics_summary_options,
    aws_api_gateway_method.analytics_trends_options,
    aws_api_gateway_method.analytics_route_efficiency_trends_options,
    # Integraciones OPTIONS (MOCK)
    aws_api_gateway_integration.circuits_options,
    aws_api_gateway_integration.circuit_containers_options,
    aws_api_gateway_integration.circuit_route_options,
    aws_api_gateway_integration.trucks_options,
    aws_api_gateway_integration.routes_comparison_options,
    aws_api_gateway_integration.optimize_circuit_options,
    aws_api_gateway_integration.analytics_summary_options,
    aws_api_gateway_integration.analytics_trends_options,
    aws_api_gateway_integration.analytics_route_efficiency_trends_options,
    # Integration responses OPTIONS
    aws_api_gateway_integration_response.circuits_options,
    aws_api_gateway_integration_response.circuit_containers_options,
    aws_api_gateway_integration_response.circuit_route_options,
    aws_api_gateway_integration_response.trucks_options,
    aws_api_gateway_integration_response.routes_comparison_options,
    aws_api_gateway_integration_response.optimize_circuit_options,
    aws_api_gateway_integration_response.analytics_summary_options,
    aws_api_gateway_integration_response.analytics_trends_options,
    aws_api_gateway_integration_response.analytics_route_efficiency_trends_options,
  ]
}

# ─────────────────────────────────────────────────────────
# API Gateway account settings — CloudWatch Logs role
#
# Configuración a nivel de cuenta (una sola vez por región).
# API Gateway necesita este rol para poder escribir logs en CloudWatch.
# Sin él, cualquier stage con access_log_settings falla con BadRequestException.
# ─────────────────────────────────────────────────────────

resource "aws_iam_role" "api_gateway_cloudwatch" {
  name = "${local.name_prefix}-apigw-cloudwatch"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "api_gateway_cloudwatch" {
  role       = aws_iam_role.api_gateway_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

# Registra el rol en la configuración de la cuenta para esta región.
# Es un recurso singleton: si ya existe un rol configurado en la cuenta,
# Terraform lo sobreescribe con este. En una cuenta compartida entre equipos
# conviene importarlo en lugar de recrearlo:
#   terraform import aws_api_gateway_account.main <account_id>
resource "aws_api_gateway_account" "main" {
  cloudwatch_role_arn = aws_iam_role.api_gateway_cloudwatch.arn

  depends_on = [aws_iam_role_policy_attachment.api_gateway_cloudwatch]
}

resource "aws_api_gateway_stage" "dev" {
  deployment_id = aws_api_gateway_deployment.smartwaste.id
  rest_api_id   = aws_api_gateway_rest_api.smartwaste.id
  stage_name    = "dev"

  description = "Entorno de desarrollo — SmartWaste MVD"

  # Logging de acceso: cada request queda registrado en CloudWatch.
  # Útil para debugging; se puede deshabilitar en prod para ahorrar costo.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway_access.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      status         = "$context.status"
      responseLength = "$context.responseLength"
    })
  }

  depends_on = [aws_api_gateway_account.main]
}

resource "aws_cloudwatch_log_group" "api_gateway_access" {
  name              = "/aws/apigateway/${local.name_prefix}-api/access"
  retention_in_days = 14
}


# ─────────────────────────────────────────────────────────
# Lambda permission — permite a API Gateway invocar la Lambda
# ─────────────────────────────────────────────────────────

# Sin este permiso la Lambda recibiría "AccessDenied" aunque el rol esté bien.
# source_arn = "*/*/*" cubre cualquier stage, método y path de esta API.
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.smartwaste.execution_arn}/*/*"
}
