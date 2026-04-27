# ─────────────────────────────────────────────────────────
# WebSocket API — SmartWaste MVD
#
# Rutas WebSocket:
#   $connect          → Lambda websocket-connect   (guarda connection_id)
#   $disconnect       → Lambda websocket-disconnect (elimina connection_id)
#   container_emptied → Lambda websocket-message   (vacía contenedor)
#
# El conductor se conecta con:
#   wss://<api>.execute-api.us-east-1.amazonaws.com/dev?truck_id=T01&circuit_id=A_DU_0101
#
# El route-optimizer llama a ws_notifier.notify_drivers() tras calcular rutas,
# que usa la Management API (endpoint HTTPS) para notificar a las conexiones activas.
# ─────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────
# DynamoDB — tabla de conexiones activas
# ─────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "connections" {
  name         = "${local.name_prefix}-connections"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "connection_id"

  attribute {
    name = "connection_id"
    type = "S"
  }

  attribute {
    name = "circuit_id"
    type = "S"
  }

  # GSI usado por ws_notifier para encontrar conexiones de un circuito
  global_secondary_index {
    name            = "circuit-index"
    hash_key        = "circuit_id"
    projection_type = "ALL"
  }

  # TTL: las conexiones que no enviaron $disconnect expiran a las 24 h
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Name = "${local.name_prefix}-connections"
  }
}


# ─────────────────────────────────────────────────────────
# WebSocket API (API Gateway V2)
# ─────────────────────────────────────────────────────────

resource "aws_apigatewayv2_api" "smartwaste_ws" {
  name                       = "${local.name_prefix}-ws"
  protocol_type              = "WEBSOCKET"
  description                = "SmartWaste MVD WebSocket API — conductor ↔ backend"

  # Selecciona la ruta según el campo "action" del body JSON del mensaje.
  # Ej: {"action": "container_emptied", ...} → ruta container_emptied
  route_selection_expression = "$request.body.action"
}

resource "aws_apigatewayv2_stage" "ws_dev" {
  api_id      = aws_apigatewayv2_api.smartwaste_ws.id
  name        = "dev"
  auto_deploy = true

  default_route_settings {
    # Throttling conservador para dev
    throttling_burst_limit = 100
    throttling_rate_limit  = 50
  }
}


# ─────────────────────────────────────────────────────────
# Lambda — websocket-connect
# ─────────────────────────────────────────────────────────

data "archive_file" "websocket_connect" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/websocket-connect"
  output_path = "${path.module}/.terraform/websocket-connect.zip"
}

resource "aws_iam_role" "websocket_connect" {
  name = "${local.name_prefix}-websocket-connect"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "websocket_connect_logs" {
  role       = aws_iam_role.websocket_connect.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "websocket_connect_dynamo" {
  name = "dynamodb-put-connection"
  role = aws_iam_role.websocket_connect.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem"]
      Resource = aws_dynamodb_table.connections.arn
    }]
  })
}

resource "aws_lambda_function" "websocket_connect" {
  function_name    = "${local.name_prefix}-websocket-connect"
  description      = "WebSocket $connect: registra la conexión del conductor en DynamoDB"
  role             = aws_iam_role.websocket_connect.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.websocket_connect.output_path
  source_code_hash = data.archive_file.websocket_connect.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      CONNECTIONS_TABLE = aws_dynamodb_table.connections.name
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.websocket_connect_logs,
    aws_iam_role_policy.websocket_connect_dynamo,
  ]
}

resource "aws_cloudwatch_log_group" "websocket_connect" {
  name              = "/aws/lambda/${aws_lambda_function.websocket_connect.function_name}"
  retention_in_days = 14
}


# ─────────────────────────────────────────────────────────
# Lambda — websocket-disconnect
# ─────────────────────────────────────────────────────────

data "archive_file" "websocket_disconnect" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/websocket-disconnect"
  output_path = "${path.module}/.terraform/websocket-disconnect.zip"
}

resource "aws_iam_role" "websocket_disconnect" {
  name = "${local.name_prefix}-websocket-disconnect"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "websocket_disconnect_logs" {
  role       = aws_iam_role.websocket_disconnect.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "websocket_disconnect_dynamo" {
  name = "dynamodb-delete-connection"
  role = aws_iam_role.websocket_disconnect.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:DeleteItem"]
      Resource = aws_dynamodb_table.connections.arn
    }]
  })
}

resource "aws_lambda_function" "websocket_disconnect" {
  function_name    = "${local.name_prefix}-websocket-disconnect"
  description      = "WebSocket $disconnect: elimina la conexión del conductor de DynamoDB"
  role             = aws_iam_role.websocket_disconnect.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.websocket_disconnect.output_path
  source_code_hash = data.archive_file.websocket_disconnect.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      CONNECTIONS_TABLE = aws_dynamodb_table.connections.name
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.websocket_disconnect_logs,
    aws_iam_role_policy.websocket_disconnect_dynamo,
  ]
}

resource "aws_cloudwatch_log_group" "websocket_disconnect" {
  name              = "/aws/lambda/${aws_lambda_function.websocket_disconnect.function_name}"
  retention_in_days = 14
}


# ─────────────────────────────────────────────────────────
# Lambda — websocket-message (container_emptied)
# ─────────────────────────────────────────────────────────

data "archive_file" "websocket_message" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/websocket-message"
  output_path = "${path.module}/.terraform/websocket-message.zip"
}

resource "aws_iam_role" "websocket_message" {
  name = "${local.name_prefix}-websocket-message"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "websocket_message_logs" {
  role       = aws_iam_role.websocket_message.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "websocket_message_dynamo" {
  name = "dynamodb-access"
  role = aws_iam_role.websocket_message.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "UpdateContainers"
        Effect   = "Allow"
        # UpdateItem para marcar fill_level=0 tras vaciado
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.containers.arn
      },
      {
        Sid      = "ReadConnection"
        Effect   = "Allow"
        # GetItem para obtener truck_id de la conexión activa
        Action   = ["dynamodb:GetItem"]
        Resource = aws_dynamodb_table.connections.arn
      },
    ]
  })
}

resource "aws_lambda_function" "websocket_message" {
  function_name    = "${local.name_prefix}-websocket-message"
  description      = "WebSocket container_emptied: registra vaciado en DynamoDB"
  role             = aws_iam_role.websocket_message.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.websocket_message.output_path
  source_code_hash = data.archive_file.websocket_message.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      CONTAINERS_TABLE  = aws_dynamodb_table.containers.name
      CONNECTIONS_TABLE = aws_dynamodb_table.connections.name
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.websocket_message_logs,
    aws_iam_role_policy.websocket_message_dynamo,
  ]
}

resource "aws_cloudwatch_log_group" "websocket_message" {
  name              = "/aws/lambda/${aws_lambda_function.websocket_message.function_name}"
  retention_in_days = 14
}


# ─────────────────────────────────────────────────────────
# Integrations y Routes
# ─────────────────────────────────────────────────────────

resource "aws_apigatewayv2_integration" "ws_connect" {
  api_id             = aws_apigatewayv2_api.smartwaste_ws.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.websocket_connect.invoke_arn
  # content_handling_strategy no se usa en WebSocket, pero se documenta
  # que el payload siempre es string (JSON serializado)
}

resource "aws_apigatewayv2_integration" "ws_disconnect" {
  api_id           = aws_apigatewayv2_api.smartwaste_ws.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.websocket_disconnect.invoke_arn
}

resource "aws_apigatewayv2_integration" "ws_message" {
  api_id           = aws_apigatewayv2_api.smartwaste_ws.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.websocket_message.invoke_arn
}

resource "aws_apigatewayv2_route" "connect" {
  api_id    = aws_apigatewayv2_api.smartwaste_ws.id
  route_key = "$connect"
  target    = "integrations/${aws_apigatewayv2_integration.ws_connect.id}"
}

resource "aws_apigatewayv2_route" "disconnect" {
  api_id    = aws_apigatewayv2_api.smartwaste_ws.id
  route_key = "$disconnect"
  target    = "integrations/${aws_apigatewayv2_integration.ws_disconnect.id}"
}

resource "aws_apigatewayv2_route" "container_emptied" {
  api_id    = aws_apigatewayv2_api.smartwaste_ws.id
  route_key = "container_emptied"
  target    = "integrations/${aws_apigatewayv2_integration.ws_message.id}"
}


# ─────────────────────────────────────────────────────────
# Lambda permissions — permite a API Gateway invocar las Lambdas
# ─────────────────────────────────────────────────────────

resource "aws_lambda_permission" "ws_connect" {
  statement_id  = "AllowWebSocketConnectInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.websocket_connect.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.smartwaste_ws.execution_arn}/*/*"
}

resource "aws_lambda_permission" "ws_disconnect" {
  statement_id  = "AllowWebSocketDisconnectInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.websocket_disconnect.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.smartwaste_ws.execution_arn}/*/*"
}

resource "aws_lambda_permission" "ws_message" {
  statement_id  = "AllowWebSocketMessageInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.websocket_message.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.smartwaste_ws.execution_arn}/*/*"
}
