# Terraform — SmartWaste MVD

AWS infrastructure defined in Terraform for the Montevideo waste collection route optimization system.

## Resources

| File | Resources |
|---|---|
| `dynamodb.tf` | 4 DynamoDB on-demand tables with GSIs and TTL |
| `iot.tf` | IoT Thing Type + sensor access policy |
| `kinesis.tf` | Kinesis Data Stream (ON_DEMAND), Firehose x2, S3 Data Lake, 6 lifecycle rules |
| `ecs.tf` | ECR repos, ECS Cluster/Task/Service for OSRM, Cloud Map, cuOpt EC2 GPU (optional) |
| `lambda.tf` | Lambdas: process-sensor-reading (SQS batch), route-optimizer (VPC, 300s timeout), api, sensor-simulator |
| `api-gateway.tf` | REST API + dev stage + CloudWatch logs |
| `websocket.tf` | WebSocket API + $connect/$disconnect/$default |
| `vpc.tf` | VPC, private subnets, NAT Gateway (conditional), VPC Endpoints for DynamoDB/S3/ECR |
| `analytics.tf` | Glue Catalog, Athena Workgroup, Glue Job ETL, S3 script upload |
| `outputs.tf` | ARNs, endpoints, env var block |

**Ingestion pipeline:** IoT Core -> SQS -> Lambda batch (100 msgs / 10s) -> DynamoDB
**Analytics archival:** IoT Core -> Kinesis Data Stream (ON_DEMAND) -> Firehose -> S3 Bronze

## cuOpt deployment modes

The route optimizer uses NVIDIA cuOpt to solve the Vehicle Routing Problem (VRP). There are two deployment modes, controlled by the `cuopt_self_hosted` variable:

### Mode 1: NVIDIA API Catalog (default)

cuOpt runs in NVIDIA's cloud. The Lambda calls the API directly over the internet via NAT Gateway.

```bash
terraform apply \
  -var='cuopt_mode=api_catalog' \
  -var='cuopt_api_key=nvapi-...'
```

| | |
|---|---|
| **Cost** | Free tier: 5,000 requests/month. NAT Gateway: ~$32/month + data transfer |
| **Latency** | ~2-5s per solve (network round-trip to NVIDIA) |
| **GPU** | Not required locally |
| **Best for** | Development, low-volume testing |

### Mode 2: Self-hosted on EC2 GPU

cuOpt runs on an EC2 `g5.2xlarge` instance (1x A10G 24GB GPU, 8 vCPU, 32GB RAM) in a private subnet. The Lambda discovers it via Cloud Map DNS at `cuopt.smartwaste.local:5000`. No NAT Gateway is created.

```bash
terraform apply -var='cuopt_self_hosted=true'
```

| | |
|---|---|
| **Cost** | ~$1.21/hr on-demand (~$870/month always-on). No NAT Gateway cost |
| **Latency** | ~200-500ms per solve (VPC-internal) |
| **GPU** | EC2 g5.2xlarge (A10G 24GB) |
| **Best for** | Production, high-volume (>5K requests/month) |

**What gets created when `cuopt_self_hosted=true`:**
- ECR repository + NGC-to-ECR image push
- EC2 g5.2xlarge with AL2 ECS GPU AMI (Docker + NVIDIA drivers pre-installed)
- IAM role with ECR pull, CloudWatch Logs, and SSM Session Manager permissions
- Cloud Map DNS registration (`cuopt.smartwaste.local` -> EC2 private IP)
- Security group rules (Lambda -> cuOpt on port 5000)
- VPC endpoints for SSM/SSMMessages/EC2Messages (for debugging via Session Manager)

**To tear down the GPU instance:**

```bash
terraform apply  # without -var='cuopt_self_hosted=true'
```

This destroys the EC2 instance, Cloud Map registration, SSM endpoints, and related resources. The Lambda falls back to `cuopt_mode` (default: `ortools`).

### Mode 3: OR-Tools fallback (no GPU, no API)

When neither `cuopt_self_hosted` nor `cuopt_mode=api_catalog` is set, the Lambda uses Google OR-Tools (CPU-only). This is the default.

```bash
terraform apply  # defaults: cuopt_mode=ortools, cuopt_self_hosted=false
```

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.6
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) with valid credentials
- Required IAM permissions: `AmazonDynamoDBFullAccess`, `AWSIoTFullAccess`

### Verify credentials

```bash
aws sts get-caller-identity
```

## Initial setup

```bash
cd terraform/

# 1. Download providers
terraform init

# 2. Preview changes (dry run)
terraform plan

# 3. Apply infrastructure
terraform apply
```

Terraform will prompt for confirmation. To skip:

```bash
terraform apply -auto-approve
```

## Environments

Behavior changes based on the `environment` variable:

| Variable | `dev` (default) | `prod` |
|---|---|---|
| Resource prefix | `smartwaste-dev` | `smartwaste` |
| Point-in-time recovery | disabled | enabled |
| Table names | `smartwaste-dev-containers` | `smartwaste-containers` |

To deploy to a specific environment:

```bash
terraform apply -var="environment=prod"
```

Or create a `terraform.tfvars` file:

```hcl
environment    = "dev"
project_prefix = "smartwaste"
```

## Generated environment variables

After `terraform apply`, get the ready-to-copy env var block:

```bash
terraform output env_block
```

Example output:
```
AWS_REGION=us-east-1
DYNAMODB_CONTAINERS_TABLE=smartwaste-dev-containers
DYNAMODB_TRUCKS_TABLE=smartwaste-dev-trucks
DYNAMODB_ROUTES_TABLE=smartwaste-dev-routes
DYNAMODB_SENSOR_READINGS_TABLE=smartwaste-dev-sensor-readings
IOT_ENDPOINT=xxxxxxxxxxxxxxx-ats.iot.us-east-1.amazonaws.com
```

Copy these values to the local `.env` and Lambda configuration.

## DynamoDB tables

### `smartwaste-[env]-containers`
Static metadata for each container (location, circuit, capacity).

| Attribute | Type | Role |
|---|---|---|
| `container_id` | String | PK |
| `circuit_id` | String | GSI `circuit-index` PK |

### `smartwaste-[env]-trucks`
Operational state for each truck.

| Attribute | Type | Role |
|---|---|---|
| `truck_id` | String | PK |
| `status` | String | GSI `status-index` PK |

`status` values: `available`, `en_route`, `maintenance`.

### `smartwaste-[env]-routes`
Routes computed by the optimizer.

| Attribute | Type | Role |
|---|---|---|
| `route_id` | String | PK |
| `circuit_id` | String | GSI `circuit-index` PK |
| `truck_id` | String | Regular attribute (no GSI) |

The truck-to-active-route association is resolved via the `active_route_id` field in the `trucks` table, updated by the optimizer after saving each route. No GSI on `truck_id` to avoid replicating the 7-day history for that single query.

### `smartwaste-[env]-sensor-readings`
Sensor readings time-series. 30-day TTL (long-term history goes to S3).

| Attribute | Type | Role |
|---|---|---|
| `container_id` | String | PK |
| `timestamp` | String (ISO 8601 UTC) | SK |
| `ttl` | Number (epoch) | TTL |

## Key configurations

| Resource | Parameter | Value |
|----------|-----------|-------|
| Lambda `route-optimizer` | Timeout | 300s (5 min) |
| Lambda `route-optimizer` | Memory | 512 MB |
| Lambda `process-sensor-reading` | SQS batch size | 100 msgs |
| Lambda `process-sensor-reading` | Max batching window | 10s |
| ECS OSRM | CPU | 2048 (2 vCPU) |
| ECS OSRM | Memory | 4096 MB (4 GB) |
| EC2 cuOpt (optional) | Instance type | g5.2xlarge (A10G 24GB GPU) |
| EC2 cuOpt (optional) | Root volume | 100 GB gp3 |
| Kinesis Data Stream | Mode | ON_DEMAND |
| S3 Lifecycle | Rules | 6 (Bronze/Silver/Gold/Staging) |

## IoT Core

### Thing Type
`smartwaste-[env]-WasteContainer` — groups all containers. Searchable attributes: `circuit_id`, `zone`, `shift`.

### Policy `smartwaste-[env]-sensor-policy`
Minimum permissions for devices/simulators:

| Action | Resource |
|---|---|
| `iot:Connect` | `client/smartwaste-*` |
| `iot:Publish`, `iot:RetainPublish` | `topic/smartwaste-*/containers/*/readings`, `topic/smartwaste-*/trucks/*/position` |
| `iot:Subscribe`, `iot:Receive` | `topicfilter/smartwaste-*/*`, `topic/smartwaste-*/*` |

To attach this policy to a device certificate:

```bash
aws iot attach-policy \
  --policy-name smartwaste-dev-sensor-policy \
  --target <certificate-arn>
```

## Terraform state

State is stored locally in `terraform/terraform.tfstate`. This file **must not be committed** (it's in `.gitignore`).

### Migration to S3 backend (when collaboration is needed)

1. Create an S3 bucket and DynamoDB table for locking (outside this module):

```bash
aws s3 mb s3://smartwaste-terraform-state-<account-id> --region us-east-1
aws dynamodb create-table \
  --table-name smartwaste-terraform-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```

2. Replace the `backend "local"` block in `main.tf`:

```hcl
backend "s3" {
  bucket         = "smartwaste-terraform-state-<account-id>"
  key            = "smartwaste/dev/terraform.tfstate"
  region         = "us-east-1"
  dynamodb_table = "smartwaste-terraform-locks"
  encrypt        = true
}
```

3. Migrate the existing state:

```bash
terraform init -migrate-state
```

## Destroy infrastructure

```bash
# Preview what would be destroyed
terraform plan -destroy

# Destroy (prompts for confirmation)
terraform destroy
```

> **Warning:** in `prod`, DynamoDB tables have Point-in-Time Recovery enabled but `prevent_destroy` is not configured. Add it if additional protection is needed.
