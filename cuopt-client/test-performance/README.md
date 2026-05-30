# cuOpt Performance Test Suite

Suite de benchmarks para medir la performance de NVIDIA cuOpt bajo carga creciente,
comparando el modo **API Catalog** (cloud) con **Self-Hosted** (EC2 GPU).

## Requisitos

```bash
pip install -r requirements.txt
```

También necesitás las dependencias del `cuopt-client/` padre (`requests`, `python-dotenv`).

## Uso rápido

### Testear contra API Catalog (cloud)

```bash
# Asegurate de tener CUOPT_API_KEY en el .env o como variable de entorno
python run_benchmarks.py --mode api_catalog --quick
```

### Testear contra Self-Hosted (EC2 GPU)

```bash
# Opción 1: Todo automatizado (lanza EC2, testea, destruye)
./deploy_cuopt_ec2.sh run-and-teardown --quick

# Opción 2: Paso a paso
./deploy_cuopt_ec2.sh launch
./deploy_cuopt_ec2.sh status
python run_benchmarks.py --mode self_hosted --server http://<IP>:5000
./deploy_cuopt_ec2.sh teardown
```

### Testear ambos modos

```bash
python run_benchmarks.py --mode both --server http://<IP>:5000
```

## Opciones del CLI

```
--mode {api_catalog,self_hosted,both}  Modo de cuOpt a testear (requerido)
--server URL                           URL del servidor self-hosted
--api-key KEY                          API key NVIDIA (default: env CUOPT_API_KEY)
--sizes 10,50,100                      Tamaños de problema (nodes)
--concurrency 1,5,10                   Niveles de concurrencia
--vehicles 2,4                         Cantidades de vehículos
--repetitions N                        Repeticiones por caso (default: 3)
--quick                                Subset reducido para pruebas rápidas
--skip-ramp                            Omitir tests de ramp-up
--containers-csv PATH                  CSV con coordenadas reales de contenedores
--output-dir PATH                      Directorio de salida (default: results/)
```

## Escenarios de test

1. **Sequential**: Envía requests uno a uno. Mide latencia individual por tamaño de problema.
2. **Concurrent**: Envía N requests simultáneos. Mide throughput y latencia bajo carga.
3. **Ramp-up** (solo self_hosted): Incrementa concurrencia gradualmente. Identifica punto de saturación.

## Gráficas generadas

| Gráfica | Descripción |
|---------|-------------|
| `latency_vs_size_*.png` | Latencia p50/p95 vs tamaño del problema |
| `latency_vs_concurrency_*.png` | Latencia p50 vs requests concurrentes |
| `throughput_vs_concurrency_*.png` | Throughput (req/s) vs concurrencia |
| `latency_distribution_*.png` | Box plot de distribución de latencias |
| `success_rate_*.png` | Tasa de éxito vs carga |

## Output

Los resultados se guardan en `results/`:
- `raw_<run_id>.json` — Resultados crudos (cada request individual)
- `summary_<run_id>.csv` — Estadísticas agregadas (p50, p95, p99, mean, throughput)
- `*.png` — Gráficas

## Deploy script (EC2 GPU)

El script `deploy_cuopt_ec2.sh` gestiona una instancia EC2 g5.2xlarge temporal:

| Comando | Acción |
|---------|--------|
| `launch` | Lanza instancia, espera health check (~5-10 min) |
| `status` | Verifica estado de la instancia y cuOpt |
| `run-tests` | Launch + ejecuta benchmarks |
| `run-and-teardown` | Launch + test + cleanup **(recomendado)** |
| `teardown` | Termina instancia y limpia Security Group |

**Costo**: ~$1.21/hora (g5.2xlarge on-demand, us-east-1).

## Rate limits (API Catalog)

El free tier de NVIDIA permite 5,000 requests/mes. La suite maneja esto con:
- Delay de 1s entre requests secuenciales
- Máximo 5 requests concurrentes
- Estimación pre-flight con advertencia si excede 200 requests
- Límite de seguridad de 500 requests por run
- Ramp-up omitido automáticamente
