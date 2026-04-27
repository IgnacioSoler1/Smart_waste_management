# Terraform — SmartWaste MVD

Infraestructura AWS definida en Terraform para el sistema de optimización de rutas de recolección de residuos de Montevideo.

## Recursos incluidos

| Archivo | Recursos |
|---|---|
| `dynamodb.tf` | 4 tablas DynamoDB on-demand con GSIs y TTL |
| `iot.tf` | IoT Thing Type + política de acceso para sensores |
| `kinesis.tf` | Kinesis Data Stream (ON_DEMAND), Firehose × 2, S3 Data Lake, lifecycle 6 reglas |
| `ecs.tf` | ECR, ECS Cluster/Task (2 vCPU / 4 GB) / Service OSRM, Cloud Map |
| `lambda.tf` | Lambdas: process-sensor-reading (SQS batch), route-optimizer (timeout 300s, VPC), api, sensor-simulator |
| `api-gateway.tf` | REST API + stage dev + CloudWatch logs |
| `websocket.tf` | WebSocket API + $connect/$disconnect/$default |
| `vpc.tf` | VPC, subnets privadas, NAT Gateway, VPC Endpoints DynamoDB/S3 |
| `analytics.tf` | Glue Catalog, Athena Workgroup, Glue Job ETL, S3 script upload |
| `outputs.tf` | ARNs, endpoints, bloque de variables de entorno |

**Pipeline de ingesta:** IoT Core → SQS → Lambda batch (100 msgs / 10s) → DynamoDB operativo
**Archivado analítico:** IoT Core → Kinesis Data Stream (ON_DEMAND) → Firehose → S3 Bronze

## Prerequisitos

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.6
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) configurado con credenciales válidas
- Permisos IAM necesarios: `AmazonDynamoDBFullAccess`, `AWSIoTFullAccess`

### Verificar credenciales

```bash
aws sts get-caller-identity
```

## Setup inicial

```bash
cd terraform/

# 1. Descargar providers
terraform init

# 2. Ver plan de cambios (sin aplicar)
terraform plan

# 3. Aplicar infraestructura
terraform apply
```

Terraform pedirá confirmación antes de crear recursos. Para saltearse la confirmación:

```bash
terraform apply -auto-approve
```

## Entornos

El comportamiento cambia según la variable `environment`:

| Variable | `dev` (default) | `prod` |
|---|---|---|
| Prefijo de recursos | `smartwaste-dev` | `smartwaste` |
| Point-in-time recovery | deshabilitado | habilitado |
| Nombres de tablas | `smartwaste-dev-containers` | `smartwaste-containers` |

Para desplegar en un entorno específico:

```bash
terraform apply -var="environment=prod"
```

O crear un archivo `terraform.tfvars`:

```hcl
environment    = "dev"
project_prefix = "smartwaste"
```

## Variables de entorno generadas

Después de `terraform apply`, obtener el bloque de variables de entorno listo para copiar:

```bash
terraform output env_block
```

Ejemplo de salida:
```
AWS_REGION=us-east-1
DYNAMODB_CONTAINERS_TABLE=smartwaste-dev-containers
DYNAMODB_TRUCKS_TABLE=smartwaste-dev-trucks
DYNAMODB_ROUTES_TABLE=smartwaste-dev-routes
DYNAMODB_SENSOR_READINGS_TABLE=smartwaste-dev-sensor-readings
IOT_ENDPOINT=xxxxxxxxxxxxxxx-ats.iot.us-east-1.amazonaws.com
```

Copiar estos valores en el `.env` local y en la configuración de las Lambdas.

## Tablas DynamoDB

### `smartwaste-[env]-containers`
Metadatos estáticos de cada contenedor (ubicación, circuito, capacidad).

| Atributo | Tipo | Rol |
|---|---|---|
| `container_id` | String | PK |
| `circuit_id` | String | GSI `circuit-index` PK |

### `smartwaste-[env]-trucks`
Estado operativo de cada camión.

| Atributo | Tipo | Rol |
|---|---|---|
| `truck_id` | String | PK |
| `status` | String | GSI `status-index` PK |

Valores de `status`: `available`, `en_route`, `maintenance`.

### `smartwaste-[env]-routes`
Rutas calculadas por el optimizador.

| Atributo | Tipo | Rol |
|---|---|---|
| `route_id` | String | PK |
| `circuit_id` | String | GSI `circuit-index` PK |
| `truck_id` | String | Atributo regular (sin GSI) |

La asociación truck → ruta activa se resuelve a través del campo `active_route_id` en la tabla `trucks`,
que el optimizer actualiza tras guardar cada ruta. No hay GSI en `truck_id` para evitar replicar
el historial de 7 días solo para esa consulta.

### `smartwaste-[env]-sensor-readings`
Time-series de lecturas de sensores. TTL de 30 días (el historial largo plazo va a S3).

| Atributo | Tipo | Rol |
|---|---|---|
| `container_id` | String | PK |
| `timestamp` | String (ISO 8601 UTC) | SK |
| `ttl` | Number (epoch) | TTL |

## Configuraciones clave

| Recurso | Parámetro | Valor |
|---------|-----------|-------|
| Lambda `route-optimizer` | Timeout | 300s (5 min) |
| Lambda `route-optimizer` | Memoria | 256 MB |
| Lambda `process-sensor-reading` | SQS batch size | 100 msgs |
| Lambda `process-sensor-reading` | Max batching window | 10s |
| ECS OSRM | CPU | 2048 (2 vCPU) |
| ECS OSRM | Memoria | 4096 MB (4 GB) |
| Kinesis Data Stream | Mode | ON_DEMAND |
| S3 Lifecycle | Reglas | 6 (Bronze/Silver/Gold/Staging) |

## IoT Core

### Thing Type
`smartwaste-[env]-WasteContainer` — agrupa todos los contenedores. Atributos buscables: `circuit_id`, `zone`, `shift`.

### Política `smartwaste-[env]-sensor-policy`
Permisos mínimos para dispositivos/simuladores:

| Acción | Recurso |
|---|---|
| `iot:Connect` | `client/smartwaste-*` |
| `iot:Publish`, `iot:RetainPublish` | `topic/smartwaste-*/containers/*/readings`, `topic/smartwaste-*/trucks/*/position` |
| `iot:Subscribe`, `iot:Receive` | `topicfilter/smartwaste-*/*`, `topic/smartwaste-*/*` |

Para adjuntar esta política a un certificado de dispositivo:

```bash
aws iot attach-policy \
  --policy-name smartwaste-dev-sensor-policy \
  --target <certificate-arn>
```

## Estado de Terraform

El estado se guarda localmente en `terraform/terraform.tfstate`. Este archivo **no debe commitearse** (está en `.gitignore`).

### Migración a backend S3 (cuando se necesite colaboración)

1. Crear bucket S3 y tabla DynamoDB para lock (fuera de este módulo):

```bash
aws s3 mb s3://smartwaste-terraform-state-<account-id> --region us-east-1
aws dynamodb create-table \
  --table-name smartwaste-terraform-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```

2. Reemplazar el bloque `backend "local"` en `main.tf`:

```hcl
backend "s3" {
  bucket         = "smartwaste-terraform-state-<account-id>"
  key            = "smartwaste/dev/terraform.tfstate"
  region         = "us-east-1"
  dynamodb_table = "smartwaste-terraform-locks"
  encrypt        = true
}
```

3. Migrar el estado existente:

```bash
terraform init -migrate-state
```

## Destruir infraestructura

```bash
# Ver qué se destruiría
terraform plan -destroy

# Destruir (pide confirmación)
terraform destroy
```

> **Atención:** en `prod`, las tablas DynamoDB tienen Point-in-Time Recovery habilitado pero `prevent_destroy` no está configurado. Agregar si se requiere protección adicional.
