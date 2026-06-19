# cuOpt / OR-Tools Benchmark Suite

Benchmarks empíricos que respaldan los hallazgos publicados en
[`docs/cuopt.md`](../../docs/cuopt.md): cuándo conviene cuOpt vs OR-Tools,
en qué régimen cada uno gana, y cuánto cuesta operacionalmente.

Todos los scripts producen **JSON crudo** en `results/` con timestamps
en el nombre, más una **tabla comparativa** en stdout.

---

## Quick start — ¿Qué querés hacer?

| Si querés... | Corré este script | Necesita |
|---|---|---|
| Ver cómo escala OR-Tools sólo, sobre varios tamaños y time-limits | `python3 benchmark_ortools.py` | nada |
| Ver cómo escala cuOpt cloud, sweep de tamaño × time_limit | `python3 benchmark_time_limits.py` | `CUOPT_API_KEY` |
| Encontrar el inflection point donde cuOpt empieza a ganarle a OR-Tools (vehículos × capacidad) | `python3 benchmark_inflection.py` | `CUOPT_API_KEY` |
| Probar problemas de 800–1,000 nodos (single problem, no consolidado) | `python3 benchmark_megascale.py` | `CUOPT_API_KEY` |
| Medir el beneficio de consolidar circuitos (cloud, 3–12 circuitos por grupo) | `python3 benchmark_consolidation_cloud.py` | `CUOPT_API_KEY` |
| Medir consolidación a escala mayor (15–30 circuitos, time windows, long/short time-limit) | `python3 benchmark_consolidation_selfhosted.py` | EC2 g5.2xlarge corriendo (ver abajo) |
| Test de concurrencia/saturación (sequential + concurrent + ramp-up) | `python3 run_benchmarks.py --mode {api_catalog,self_hosted}` | `CUOPT_API_KEY` o `--server` |

---

## Script taxonomy

| Script | Propósito |
|---|---|
| `benchmark_ortools.py` | OR-Tools sweep sizes × time-limits, sin tocar cuOpt. |
| `benchmark_time_limits.py` | cuOpt cloud sweep sizes × time-limits. Mide si cuOpt termina early o exhausta el TL. |
| `benchmark_inflection.py` | Matriz vehículos × slack de capacidad, busca el régimen donde cuOpt empieza a ganar (Regime B). |
| `benchmark_megascale.py` | cuOpt vs OR-Tools sobre UN problema grande (800–1,000 nodos), 10–20 vehículos. |
| `benchmark_consolidation_cloud.py` | Consolidación de varios circuitos en 1 solve, comparando 3 modos (cuOpt cons / OR-Tools cons / OR-Tools sequential). Cloud, 3–12 circuitos por default. |
| `benchmark_consolidation_selfhosted.py` | Lo mismo a mayor escala (15–30 circuitos), más SUITE A (long TL), B (time windows), C (short TL). Requiere cuOpt EC2. |
| `run_benchmarks.py` + `benchmark_runner.py` | Harness general para tests de concurrencia y ramp-up. Acepta `--mode api_catalog`/`--mode self_hosted`. |
| `deploy_cuopt_ec2.sh` | Lanza/destruye un EC2 g5.2xlarge con cuOpt Docker. Usado por los tests self-hosted. |

**Auxiliares (no se ejecutan directo):**
- `config.py` — constantes compartidas (problem sizes, vehicle counts, depots).
- `problem_generator.py` — genera problemas VRP sintéticos con coordenadas reales o aleatorias.
- `result_collector.py` — agrega métricas (p50/p95/p99, throughput).
- `visualizer.py` — genera PNG con matplotlib.

---

## Setup

```bash
pip install -r requirements.txt
```

Las claves se leen del entorno o de `cuopt-client/.env` (el dotenv loader
parsea automáticamente):

```bash
# Para cualquier benchmark cloud (cuOpt API Catalog):
export CUOPT_API_KEY=nvapi-...

# Para self-hosted: pasar --server o dejar que se lea de results/.ec2_state.json
```

---

## Workflow self-hosted (EC2 g5.2xlarge)

⚠️ **Costo: ~$1.21/hora on-demand.** Acordate de destruir.

```bash
# 1. Lanzar (espera ~5-10 min hasta que cuOpt esté healthy)
./deploy_cuopt_ec2.sh launch

# 2. Correr cualquier benchmark contra el servidor
python3 benchmark_consolidation_selfhosted.py             # corre todas las suites
python3 benchmark_consolidation_selfhosted.py --suite C   # solo SUITE C (short TL cliff)
python3 benchmark_consolidation_selfhosted.py --server http://<IP>:5000 --suite B

# 3. Destruir SIEMPRE cuando termines
./deploy_cuopt_ec2.sh teardown

# Alternativa: launch + run + teardown en un comando (ideal para CI):
./deploy_cuopt_ec2.sh run-and-teardown --quick
```

El script de deploy también soporta `status`, `run-tests`, etc. — ver
`./deploy_cuopt_ec2.sh help`.

**Capacity issues**: g5.2xlarge frecuentemente devuelve
`InsufficientInstanceCapacity` en todas las AZ de us-east-1 simultáneamente.
Si pasa, esperar 20–60 min y reintentar. **No usar g5.xlarge** (incompatible
con cuOpt Docker).

---

## Suites del `benchmark_consolidation_selfhosted.py`

Este es el harness más completo. Acepta `--suite {0,A,B,C,all}`:

| Suite | Qué mide | Hallazgo principal |
|---|---|---|
| 0 | Consolidación a escala 15, 20, 25, 30 circuitos (1186–2371 nodos) sin time windows | Δ% cuOpt vs OR-Tools consolidado: 1.7–9.8%. Wall-clock speedup vs OR-Tools sequential: 8.4–11.3×. |
| A | Mismo problema (g=20=1581 nodos) con TL ∈ {10, 30, 60, 120, 180}s | Más tiempo NO amplifica el gap (~5% se mantiene constante) |
| B | Mismo problema + time windows (2h ventana en 8h shift) | **Δ% +20.77% — la amplificación más grande del estudio** |
| C | Mismo problema con TL ∈ {1, 2, 3, 5, 7, 10}s, 3 seeds | Inverted-U: peak +6.33% a TL=3s, −6.30% a TL=1s |

Todos los hallazgos están documentados en
[`docs/cuopt.md` §5.1](../../docs/cuopt.md).

---

## Output

| Path | Contenido |
|---|---|
| `results/raw_<run_id>.json` | Resultados crudos request-por-request (formato del `result_collector`). |
| `results/summary_<run_id>.csv` | Estadísticas agregadas (p50/p95/p99/mean/throughput). |
| `results/consolidation_cloud_<ts>.json` | Output de `benchmark_consolidation_cloud.py`. |
| `results/supermega_extended_<ts>.json` | Output de `benchmark_consolidation_selfhosted.py`. |
| `results/inflection_<ts>.json` | Output de `benchmark_inflection.py`. |
| `results/megascale_<ts>.json` | Output de `benchmark_megascale.py`. |
| `results/*.png` | Gráficas generadas por `visualizer.py` (solo `run_benchmarks.py`). |
| `results/.ec2_state.json` | Persistido por `deploy_cuopt_ec2.sh launch`; consumido por scripts self-hosted vía `--server`. |

Cada JSON es **auto-descriptivo**: contiene timestamps, parámetros del run
y métricas por configuración. Podés re-procesarlo con cualquier herramienta
estándar.

---

## Cross-references con cuopt.md

Mapeo de qué script produjo qué sección en
[`docs/cuopt.md`](../../docs/cuopt.md):

| Sección de cuopt.md | Script que generó los números |
|---|---|
| §2 Regime A | `benchmark_time_limits.py` + `benchmark_inflection.py` (subset 1v) |
| §2 Regime B | `benchmark_inflection.py` (multi-vehicle) |
| §2 Regime C | `benchmark_megascale.py` |
| §5.1 Empirical validation (3, 5 circuitos) | `benchmark_consolidation_cloud.py --group-sizes 3,5` |
| §5.1 Pushing further (5–12 circuitos) | `benchmark_consolidation_cloud.py --group-sizes 5,8,10,12` |
| §5.1 Beyond the cloud limit (15–30 circuitos) | `benchmark_consolidation_selfhosted.py --suite 0` |
| §5.1 Long time budget | `benchmark_consolidation_selfhosted.py --suite A` |
| §5.1 Time windows (la decisiva) | `benchmark_consolidation_selfhosted.py --suite B` |
| §5.1 Short time budget cliff | `benchmark_consolidation_selfhosted.py --suite C` |

---

## Rate limits (cuOpt cloud)

NVIDIA free tier: 5,000 requests/mes. Los scripts cloud manejan esto con:
- Delay de 1 s entre requests secuenciales
- Máximo 5 requests concurrentes en `run_benchmarks.py`
- Estimación pre-flight con advertencia si excede el threshold
- Límite de seguridad de 500 requests por run

Para tests más agresivos, usar self-hosted (sin límite de requests).
