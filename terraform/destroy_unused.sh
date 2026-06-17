#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# destroy_unused.sh
#
# Destruye los recursos AWS que NO son necesarios para que el
# frontend-dashboard funcione, manteniendo el bucket S3 del data lake.
#
# QUÉ MANTIENE (no incluido abajo):
#   - aws_s3_bucket.data_lake (data lake con históricos)
#   - aws_dynamodb_table.containers / trucks / routes (lecturas dashboard)
#   - aws_api_gateway_*, aws_lambda_function.api (REST API del dashboard)
#   - aws_lambda_function.route_optimizer + VPC + NAT + WebSocket + route_firehose
#       (no se pueden destruir sin tocar código por dependencias del api Lambda)
#
# QUÉ DESTRUYE:
#   - EventBridge schedules (sensor simulator + route optimizer triggers)
#   - Sensor simulator Lambda + IAM
#   - Process-sensor-reading Lambda + SQS + IoT topic rule + IAM
#   - JITR provisioning Lambda + IoT thing type + IoT policy + IAM
#   - Kinesis sensor stream + sensor Firehose + sus IAM
#   - ECS cluster + OSRM service + ECR repos + service discovery
#   - cuOpt IAM + SGs (la EC2 no está deployed)
# NOTA: aws_ecr_repository.cuopt se MANTIENE porque su grafo cascadea al
#   route_optimizer Lambda (vía depends_on a aws_instance.cuopt con count=0).
#   Costo: ~centavos/mes si tiene imagen subida.
#   - Glue job + trigger + catalog tables + database + Athena workgroup + IAM
#   - DynamoDB table sensor_readings
#   - VPC interface endpoints prescindibles (ecr_api, ecr_dkr, kinesis_firehose, logs)
#
# COSTO ESTIMADO AHORRADO: ECS Fargate + Glue runs + Kinesis + VPC endpoints
# (NAT Gateway ~$32/mo SE MANTIENE — requiere edición de código para destruir)
# ─────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

terraform destroy \
  -target=aws_cloudwatch_event_target.sensor_simulator \
  -target=aws_cloudwatch_event_rule.sensor_simulator_schedule \
  -target=aws_lambda_permission.eventbridge_invoke_sensor_simulator \
  -target=aws_cloudwatch_event_target.route_optimizer \
  -target=aws_cloudwatch_event_rule.route_optimizer_schedule \
  -target=aws_lambda_permission.eventbridge_invoke_route_optimizer \
  \
  -target=aws_lambda_function.sensor_simulator \
  -target=aws_cloudwatch_log_group.sensor_simulator \
  -target=aws_iam_role_policy.sensor_simulator_iot_publish \
  -target=aws_iam_role_policy.sensor_simulator_dynamo \
  -target=aws_iam_role_policy_attachment.sensor_simulator_logs \
  -target=aws_iam_role.sensor_simulator \
  -target=null_resource.sensor_simulator_build \
  \
  -target=aws_lambda_event_source_mapping.sensor_sqs \
  -target=aws_lambda_function.process_sensor_reading \
  -target=aws_cloudwatch_log_group.process_sensor_reading \
  -target=aws_iam_role_policy.process_sensor_reading_sqs \
  -target=aws_iam_role_policy.process_sensor_reading_dynamo \
  -target=aws_iam_role_policy_attachment.process_sensor_reading_logs \
  -target=aws_iam_role.process_sensor_reading \
  -target=aws_iot_topic_rule.smartwaste_sensor_ingest \
  -target=aws_cloudwatch_log_group.iot_sensor_errors \
  -target=aws_iam_role_policy.iot_sensor_ingest_logs \
  -target=aws_iam_role_policy.iot_sensor_ingest_sqs \
  -target=aws_iam_role.iot_sensor_ingest \
  -target=aws_sqs_queue_policy.sensor_readings_iot \
  -target=aws_sqs_queue.sensor_readings \
  \
  -target=aws_iot_topic_rule.jitr_provision \
  -target=aws_lambda_permission.jitr_iot_invoke \
  -target=aws_cloudwatch_log_group.jitr_provisioning \
  -target=aws_lambda_function.jitr_provisioning \
  -target=aws_iam_role_policy.jitr_lambda_iot \
  -target=aws_iam_role_policy_attachment.jitr_lambda_logs \
  -target=aws_iam_role.jitr_lambda \
  -target=aws_iot_policy.sensor_policy \
  -target=aws_iot_thing_type.waste_container \
  \
  -target=aws_kinesis_firehose_delivery_stream.sensor_firehose \
  -target=aws_iam_role_policy.firehose_sensor \
  -target=aws_iam_role.firehose_sensor \
  -target=aws_iam_role_policy.iot_kinesis_ingest \
  -target=aws_iam_role.iot_kinesis_ingest \
  -target=aws_kinesis_stream.sensor_stream \
  \
  -target=aws_ecs_service.osrm \
  -target=aws_ecs_task_definition.osrm \
  -target=aws_ecs_cluster_capacity_providers.main \
  -target=aws_ecs_cluster.main \
  -target=aws_service_discovery_service.osrm \
  -target=aws_service_discovery_service.cuopt \
  -target=aws_service_discovery_private_dns_namespace.smartwaste \
  -target=aws_cloudwatch_log_group.osrm \
  -target=aws_iam_role_policy_attachment.ecs_task_execution \
  -target=aws_iam_role.ecs_task_execution \
  -target=aws_security_group_rule.osrm_from_lambda \
  -target=aws_security_group_rule.lambda_to_osrm \
  -target=aws_security_group_rule.lambda_https \
  -target=aws_security_group.osrm \
  \
  -target=aws_iam_instance_profile.cuopt \
  -target=aws_iam_role_policy_attachment.cuopt_ec2_ssm \
  -target=aws_iam_role_policy.cuopt_ec2_logs \
  -target=aws_iam_role_policy.cuopt_ec2_ecr \
  -target=aws_iam_role.cuopt_ec2 \
  -target=aws_security_group_rule.cuopt_endpoint_https \
  -target=aws_security_group_rule.cuopt_from_lambda \
  -target=aws_security_group_rule.lambda_to_cuopt \
  -target=aws_security_group.cuopt \
  -target=aws_cloudwatch_log_group.cuopt \
  \
  -target=null_resource.cuopt_image_push \
  -target=aws_ecr_lifecycle_policy.cuopt \
  -target=null_resource.osrm_image_push \
  -target=aws_ecr_lifecycle_policy.osrm \
  -target=aws_ecr_repository.osrm \
  \
  -target=aws_glue_trigger.daily_analytics \
  -target=aws_glue_job.daily_analytics \
  -target=aws_glue_catalog_table.sensor_readings \
  -target=aws_glue_catalog_table.sensor_readings_parquet \
  -target=aws_glue_catalog_table.route_results \
  -target=aws_glue_catalog_table.route_results_parquet \
  -target=aws_glue_catalog_database.analytics \
  -target=aws_athena_workgroup.analytics \
  -target=aws_iam_role_policy.glue_etl_access \
  -target=aws_iam_role_policy_attachment.glue_etl_service \
  -target=aws_iam_role.glue_etl \
  -target=aws_s3_object.glue_etl_script \
  \
  -target=aws_dynamodb_table.sensor_readings \
  \
  -target=aws_vpc_endpoint.ecr_api \
  -target=aws_vpc_endpoint.ecr_dkr \
  -target=aws_vpc_endpoint.kinesis_firehose \
  -target=aws_vpc_endpoint.logs
