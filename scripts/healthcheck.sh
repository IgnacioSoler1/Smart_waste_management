#!/usr/bin/env bash
# healthcheck.sh — SmartWaste MVD: chequeo de salud del pipeline en AWS (solo lectura).
#
# Uso:
#   bash scripts/healthcheck.sh                 # últimas 24 h
#   bash scripts/healthcheck.sh 2026-09-27      # además, cobertura diaria desde esa fecha
#
# Revisa: Lambdas (invocaciones/errores), schedules de EventBridge, OSRM en ECS,
# logs del route-optimizer, cola SQS, Firehose → S3 (Bronze), Glue (Silver/Gold),
# archivos Gold diarios y que las rutas usen ambos depósitos (fix de depósito).

set -uo pipefail

PROFILE="${AWS_PROFILE:-personal-classify}"
REGION="${AWS_REGION:-us-east-1}"
P="smartwaste-dev"
BUCKET="smartwaste-data-lake-dev"
SINCE_DATE="${1:-}"
AWS=(aws --profile "$PROFILE" --region "$REGION")

ts_ago() {  # ts_ago <horas>  → ISO UTC (macOS y Linux)
  date -u -v-"$1"H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "$1 hours ago" +%Y-%m-%dT%H:%M:%SZ
}
ms_ago() { python3 -c "import time; print(int((time.time()-$1*3600)*1000))"; }
hr() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

START=$(ts_ago 24); END=$(ts_ago 0)

hr "0. DynamoDB — ¿hay datos cargados?"
for t in containers trucks routes; do
  n=$("${AWS[@]}" dynamodb scan --table-name "$P-$t" --select COUNT --query Count --output text 2>/dev/null \
      | awk '{s+=$1} END {print s+0}')   # el scan pagina: sumar los conteos de cada página
  printf '%-30s %8s ítems\n' "$P-$t" "$n"
done
echo "Esperado: containers ≈ 10937. Si es 0 → correr scripts/post-deploy.sh (las tablas se recrearon vacías)."
echo "(trucks = 0 es normal: el optimizador usa camiones virtuales.)"

hr "1. Lambdas — invocaciones / errores / throttles (últimas 24 h)"
printf '%-40s %10s %8s %9s\n' FUNCTION INVOCATIONS ERRORS THROTTLES
for f in route-optimizer sensor-simulator process-sensor-reading api websocket-connect websocket-message osrm-matrix-export; do
  stat() {
    "${AWS[@]}" cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name "$1" \
      --dimensions Name=FunctionName,Value="$P-$f" --start-time "$START" --end-time "$END" \
      --period 86400 --statistics Sum --query 'sum(Datapoints[].Sum)' --output text 2>/dev/null
  }
  printf '%-40s %10s %8s %9s\n' "$P-$f" "$(stat Invocations)" "$(stat Errors)" "$(stat Throttles)"
done
echo "Esperado: route-optimizer ≈ 96/día, sensor-simulator ≈ 144/día, ERRORS = 0."

hr "2. Schedules de EventBridge"
for r in route-optimizer-schedule sensor-simulator-schedule; do
  "${AWS[@]}" events describe-rule --name "$P-$r" --query '[Name,State,ScheduleExpression]' --output text
done
"${AWS[@]}" glue get-trigger --name "$P-daily-analytics" --query 'Trigger.[Name,State,Schedule]' --output text

hr "3. OSRM en ECS Fargate"
"${AWS[@]}" ecs describe-services --cluster "$P-cluster" --services "$P-osrm" \
  --query 'services[0].{status:status,desired:desiredCount,running:runningCount,lastEvent:events[0].message}' --output table

hr "4. route-optimizer — últimas ejecuciones y errores (24 h)"
LG="/aws/lambda/$P-route-optimizer"
echo "-- Resúmenes de las últimas ejecuciones:"
"${AWS[@]}" logs filter-log-events --log-group-name "$LG" --start-time "$(ms_ago 24)" \
  --filter-pattern '?"Completado" ?"Sin circuitos activos"' --query 'events[-8:].message' --output text | tr '\t' '\n' | sed 's/^/   /'
echo "-- Errores / fallos de solver, OSRM o cuOpt (conteo y últimos 5):"
"${AWS[@]}" logs filter-log-events --log-group-name "$LG" --start-time "$(ms_ago 24)" \
  --filter-pattern '?ERROR ?"Solver error" ?"OSRM error" ?"Sin solución" ?Traceback ?"Task timed out"' \
  --query 'events[].eventId' --output text | wc -w | tr -d ' ' 
"${AWS[@]}" logs filter-log-events --log-group-name "$LG" --start-time "$(ms_ago 24)" \
  --filter-pattern '?ERROR ?"Solver error" ?"OSRM error" ?"Sin solución" ?Traceback ?"Task timed out"' \
  --query 'events[-5:].message' --output text | tr '\t' '\n' | cut -c1-220 | sed 's/^/   /'

hr "5. Ingesta — cola SQS"
URL=$("${AWS[@]}" sqs get-queue-url --queue-name "$P-sensor-readings" --query QueueUrl --output text)
"${AWS[@]}" sqs get-queue-attributes --queue-url "$URL" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
  --query Attributes --output table
echo "Esperado: pocos mensajes en cola (si crece sin parar, process-sensor-reading no da abasto)."

hr "6. Firehose → S3 Bronze (hoy, UTC)"
TODAY_PATH=$(date -u +year=%Y/month=%m/day=%d)
for pref in sensor-readings route-results; do
  n=$("${AWS[@]}" s3 ls "s3://$BUCKET/$pref/$TODAY_PATH/" --recursive 2>/dev/null | wc -l | tr -d ' ')
  printf '%-18s %6s archivos hoy\n' "$pref" "$n"
done
for pref in sensor-readings-errors route-results-errors; do
  n=$("${AWS[@]}" s3 ls "s3://$BUCKET/$pref/" --recursive 2>/dev/null | wc -l | tr -d ' ')
  printf '%-22s %6s archivos (debería ser 0)\n' "$pref" "$n"
done

hr "7. Glue — últimas corridas del job diario"
"${AWS[@]}" glue get-job-runs --job-name "$P-daily-analytics" --max-items 5 \
  --query 'JobRuns[].[StartedOn,JobRunState,ExecutionTime,ErrorMessage]' --output table

hr "8. Gold — resúmenes diarios en S3"
"${AWS[@]}" s3 ls "s3://$BUCKET/analytics-results/daily/" | tail -10
if [[ -n "$SINCE_DATE" ]]; then
  echo "-- Cobertura desde $SINCE_DATE (circuitos con ruta y mejora media por día):"
  python3 - "$PROFILE" "$REGION" "$BUCKET" "$SINCE_DATE" <<'PY'
import json, sys, boto3
profile, region, bucket, since = sys.argv[1:]
s3 = boto3.Session(profile_name=profile, region_name=region).client("s3")
keys = sorted(o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="analytics-results/daily/")
              for o in p.get("Contents", []) if o["Key"].endswith(".json"))
for k in keys:
    day = k.rsplit("/", 1)[1][:-5]
    if day < since:
        continue
    g = json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
    re_ = (g.get("route_efficiency") or {}).get("summary", {})
    print(f"   {day}: circuits_with_routes={re_.get('circuits_with_routes')}  "
          f"avg_dist_improvement={re_.get('avg_distance_improvement_pct')}%  "
          f"avg_fill={g.get('summary', {}).get('avg_fill_level')}")
PY
fi

hr "9. Fix de depósito — rutas activas por depósito (DynamoDB)"
for dep in "felipe_cardoso:-56.0967" "ruta_102:-56.2645"; do
  name=${dep%%:*}; lon=${dep#*:}
  n=$("${AWS[@]}" dynamodb scan --table-name "$P-routes" --select COUNT \
      --filter-expression "#s = :a AND depot_lon = :lon" \
      --expression-attribute-names '{"#s":"status"}' \
      --expression-attribute-values "{\":a\":{\"S\":\"active\"},\":lon\":{\"N\":\"$lon\"}}" \
      --query Count --output text 2>/dev/null | awk '{s+=$1} END {print s+0}')
  printf '%-16s %6s rutas activas\n' "$name" "$n"
done
echo "Esperado: ambos > 0. Si ruta_102 = 0, el fix de depósito no está activo."

echo
echo "Listo. Revisar especialmente: ERRORS ≠ 0 (1), running = 0 en OSRM (3), errores del solver (4), Glue FAILED (7)."
