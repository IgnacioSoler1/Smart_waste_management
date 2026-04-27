# NVIDIA cuOpt — Implementación en SmartWaste MVD

## Índice

1. [Qué es el problema que resolvemos](#1-qué-es-el-problema-que-resolvemos)
2. [Arquitectura del solver](#2-arquitectura-del-solver)
3. [Módulos del sistema](#3-módulos-del-sistema)
4. [Pipeline de optimización paso a paso](#4-pipeline-de-optimización-paso-a-paso)
5. [El payload de cuOpt en detalle](#5-el-payload-de-cuopt-en-detalle)
6. [Sistema de prizes (paradas opcionales)](#6-sistema-de-prizes-paradas-opcionales)
7. [Modos de operación](#7-modos-de-operación)
8. [Polling y modelo asíncrono de la API](#8-polling-y-modelo-asíncrono-de-la-api)
9. [Fallback OR-Tools](#9-fallback-or-tools)
10. [Restricciones y limitaciones conocidas](#10-restricciones-y-limitaciones-conocidas)
11. [Variables de entorno](#11-variables-de-entorno)
12. [Testing](#12-testing)

---

## 1. Qué es el problema que resolvemos

SmartWaste MVD resuelve un **CVRP** (Capacitated Vehicle Routing Problem): dados N contenedores y K camiones, encontrar el conjunto de rutas de menor costo total tal que:

- Cada ruta empieza y termina en el depósito.
- La suma de los pesos recogidos en cada ruta no supera la capacidad del camión.
- Cada contenedor prioritario es visitado exactamente una vez.
- Contenedores con bajo nivel de llenado son opcionales (se visitan si el desvío es menor que su "prize").

Montevideo tiene ~13.000 contenedores distribuidos en 117 circuitos de ~100 contenedores c/u. El optimizer se ejecuta **cada 15 minutos por turno activo** (mañana 06-14 h, tarde 14-22 h, noche 22-06 h).

---

## 2. Arquitectura del solver

```
handler.py (Lambda)
    │
    ├── DynamoDB → leer contenedores con fill_level > 20% del circuito
    ├── DynamoDB → leer camiones idle del circuito
    │
    ├── constraints.py
    │       estimate_demand_kg()   → kg de residuos por contenedor
    │       calculate_prize()      → prioridad del contenedor
    │
    ├── osrm_client.py (OSRM en ECS Fargate, VPC privada)
    │       get_road_bearings()    → alineación de la calle en cada contenedor
    │       get_distance_matrix()  → matriz N×N de duraciones (segundos)
    │
    └── vrp_solver.py
            CuOptSolver            → NVIDIA cuOpt (GPU) [modo producción]
            ORToolsSolver          → Google OR-Tools (CPU) [fallback / dev]
```

**Flujo de datos resumido:**

```
contenedores + camiones (DynamoDB)
        ↓
  build_problem()                → locations[], demands[], prizes[]
        ↓
  OSRM Table API                 → cost_matrix[N×N] en segundos
        ↓
  cuOpt / OR-Tools solve_vrp()   → routes {vehicle_id: [node_indices]}
        ↓
  OSRM Route API                 → geometría real de calles (GeoJSON)
        ↓
  DynamoDB put_item()            → ruta persiste como "active"
        ↓
  WebSocket notify_drivers()     → push al conductor en tiempo real
```

---

## 3. Módulos del sistema

### `constraints.py` — Lógica de dominio

| Función | Propósito |
|---------|-----------|
| `estimate_demand_kg(fill_level, capacity_liters)` | Convierte fill_level (%) → kg usando densidad 0.30 kg/L. Un contenedor de 2.400 L lleno al 100% pesa ~720 kg. |
| `calculate_prize(fill_level)` | Asigna un valor de prioridad al contenedor. Este valor compite contra el costo de desvío en segundos. |
| `get_time_window(shift)` | Devuelve (earliest_s, latest_s) por turno para futuras restricciones de tiempo. |

**Tabla de prizes:**

| Fill level | Prize | Significado |
|-----------|-------|-------------|
| > 90% | 10.000 | Prácticamente obligatorio — overflow inminente |
| > 60% | 3.000 | Alta prioridad — desvío de hasta ~50 min justificado |
| 35–60% | 200–1500 (lineal) | Visita si queda de paso (~3–25 min de desvío aceptado) |
| ≤ 35% | 0 | No incluir en el problema |

> **Por qué la unidad son segundos:** La cost_matrix viene de OSRM en segundos. Si un contenedor tiene prize=600, el solver lo visita solo si el desvío adicional es menor a 600 segundos (10 minutos). Esto hace los prizes directamente comparables con los costos de ruta.

---

### `osrm_client.py` — Cliente OSRM

OSRM (Open Source Routing Machine) está desplegado en ECS Fargate dentro de la VPC privada con datos de Uruguay (OpenStreetMap). Proporciona distancias y tiempos reales por calles.

**Inversión de coordenadas:** El proyecto usa `(latitud, longitud)` internamente, pero OSRM espera `(longitud, latitud)`. La conversión se hace en `_to_osrm_coord()` de manera transparente.

| Método | API OSRM | Uso |
|--------|---------|-----|
| `get_road_bearings()` | `/route/v1/driving/` | Calcula la orientación de la calle en cada contenedor para mejorar el snap al grafo. Hace una ruta corta ~30m al norte y extrae `bearing_after` del primer maneuver. |
| `get_distance_matrix()` | `/table/v1/driving/` | Genera la matriz N×N de duraciones (segundos) y distancias (metros). Máximo 500 nodos por llamada. |
| `get_route()` | `/route/v1/driving/` | Obtiene la geometría GeoJSON de la ruta final para renderizar en el mapa del conductor. |

**Fallback Haversine:** Si OSRM no está disponible y `OSRM_FALLBACK=haversine`, se calcula una matriz aproximada con la fórmula haversine × 1.35 (factor de detour urbano) a 30 km/h. Útil para desarrollo local sin Docker.

---

### `vrp_solver.py` — Solvers VRP

Expone una interfaz única (`solve_vrp`) con dos implementaciones intercambiables:

```python
result = solver.solve_vrp(
    cost_matrix      = [[...], ...],   # N×N, enteros (segundos)
    num_vehicles     = 2,
    demands          = [0, 216, 432, ...],  # kg por nodo (0 en depósitos)
    capacities       = [25000, 25000],      # kg por camión
    depot_start_idx  = 0,
    depot_end_idx    = 0,             # puede ser diferente para multi-depot
    time_limit       = 10,            # segundos de cómputo del solver
    prizes           = [0, 10000, 3000, ...],  # opcional
)
# → {"routes": {0: [0,3,7,0], 1: [0,5,2,0]}, "total_cost": 1234,
#    "status": "OPTIMAL", "solver": "cuopt"}
```

---

## 4. Pipeline de optimización paso a paso

Para cada circuito, `_optimize_circuit()` ejecuta:

```
a) DynamoDB query (circuit-index GSI)
   → contenedores con status="active" y fill_level > 20%
   → se dividen en obligatorios (>60%) y opcionales (20-60%)

b) Skip check
   → si n_mandatory < 5: no vale la pena, retorna {status: "skipped"}

c) DynamoDB query (status-index GSI)
   → camiones con status="idle" para el circuito
   → sin camiones reales: se estiman vehículos virtuales según demanda total

d) build_problem()
   → locations = [depot] + [container_locs] + [depot]
   → demands   = [0] + [kg_i] + [0]
   → prizes    = [0] + [prize_i] + [0]

e) OSRM get_road_bearings()
   → bearing de la calle en cada ubicación (mejora calidad del snap)

f) OSRM get_distance_matrix()
   → cost_matrix[N×N] en segundos (enteros)

g) solver.solve_vrp()
   → CuOpt API o OR-Tools
   → resultado: rutas por vehículo + costo total + status

h) _supersede_routes()
   → marca rutas previas "active" de esos camiones como "superseded"

i) _save_route() por cada vehículo
   → OSRM Route API para geometría real de calles
   → DynamoDB put_item con stops[], geometría GeoJSON, métricas

j) notify_drivers() via WebSocket
   → push a conductores conectados (fire-and-forget)
```

---

## 5. El payload de cuOpt en detalle

La API de cuOpt recibe un JSON con la siguiente estructura. Este es el payload que construye `_build_payload()`:

```json
{
  "action": "cuOpt_OptimizedRouting",
  "data": {
    "cost_matrix_data": {
      "data": {
        "1": [[0, 120, 300, ...], [120, 0, 180, ...], ...]
      }
    },
    "cost_waypoint_graph_data": null,
    "travel_time_matrix_data": null,
    "travel_time_waypoint_graph_data": null,

    "fleet_data": {
      "vehicle_ids": ["veh-0", "veh-1"],
      "vehicle_locations": [[0, 0], [0, 0]],
      "capacities": [[25000, 25000]],
      "vehicle_time_windows": [[0, 100000], [0, 100000]],
      "vehicle_types": [1, 1]
    },

    "task_data": {
      "task_ids": ["task-1", "task-2", "task-3"],
      "task_locations": [1, 2, 3],
      "demand": [[216, 432, 576]],
      "prizes": [3000, 10000, 600]
    },

    "solver_config": {
      "time_limit": 10.0,
      "objectives": {
        "cost": 1,
        "travel_time": 0,
        "variance_route_size": 0,
        "variance_route_service_time": 0,
        "prize": 1
      },
      "verbose_mode": false,
      "error_logging": true
    }
  },
  "client_version": ""
}
```

**Aspectos importantes del payload:**

| Campo | Detalle |
|-------|---------|
| `cost_matrix_data.data["1"]` | La key `"1"` define el tipo de vehículo. Todos los vehículos usan `vehicle_types: [1, 1, ...]` para referenciar esta matriz. |
| `capacities` | Formato `[[cap_v0, cap_v1, ...]]` — lista de listas donde la primera dimensión es la dimensión de capacidad (aquí solo kg). |
| `demand` | Formato `[[dem_t0, dem_t1, ...]]` — igual que capacities, solo la dimensión kg. |
| `vehicle_locations` | `[[depot_start, depot_end]]` — cada vehículo empieza y termina en el depósito (nodo 0 del cost_matrix). |
| `task_locations` | Índices en la cost_matrix que corresponden a las tareas (los contenedores). **Los depósitos no son tareas.** |
| `prizes` | Lista paralela a `task_ids`. Si se incluyen, el `objectives.prize` debe ser 1. |
| `vehicle_time_windows` | Se usa una ventana amplia `[0, time_limit × 10000]` porque las restricciones de turno se manejan a nivel de Lambda, no de nodo. |

**Respuesta de cuOpt:**

```json
{
  "response": {
    "solver_response": {
      "status": 0,
      "solution_cost": 4567.8,
      "vehicle_data": {
        "veh-0": {
          "task_id":       ["task-3", "task-1"],
          "route":         [0, 3, 1, 0],
          "arrival_stamp": [0.0, 5.2, 8.7, 12.1]
        },
        "veh-1": {
          "task_id":       ["task-2"],
          "route":         [0, 2, 0],
          "arrival_stamp": [0.0, 3.1, 6.2]
        }
      }
    }
  }
}
```

**Mapeo de status numérico:**

| status | Significado | Nuestra etiqueta |
|--------|-------------|-----------------|
| 0 | Solución encontrada (óptima o muy buena) | `"OPTIMAL"` |
| 1 | Solución parcial (no todos los nodos visitados) | `"FEASIBLE"` |
| < 0 | Error / infactible | `"INFEASIBLE"` |

> **Nota:** cuOpt no garantiza optimalidad matemática para instancias grandes. El status `0` indica que el solver finalizó sin errores y encontró la mejor solución dentro del `time_limit`.

---

## 6. Sistema de prizes (paradas opcionales)

El mecanismo de prizes convierte el CVRP estándar en un **Prize-Collecting VRP (PC-VRP)**: el solver puede decidir **no visitar** un nodo si el costo del desvío supera su prize.

**En OR-Tools:** se implementa con `AddDisjunction(node, penalty=prize)`. Si el solver no visita el nodo, paga `penalty` en la función objetivo. Visita el nodo si el desvío cuesta menos que el penalty.

**En cuOpt:** se pasa el campo `prizes` en `task_data` y se activa `objectives.prize: 1`. cuOpt maneja internamente la lógica de prize-collecting.

**Ejemplo práctico:**

```
Contenedor A: fill=92% → prize=10000 → el solver pagaría 10000s de penalidad si lo salta
             → Equivale a ~2.77 horas de penalidad → siempre se visita

Contenedor B: fill=45% → prize=975   → el solver lo salta si el desvío supera 975s (~16 min)
             → En una zona densa con contenedores cercanos probablemente se visita
             → En la periferia con un desvío grande, se salta

Contenedor C: fill=28% → prize=0     → nunca se incluye en el problema
```

Esta lógica permite que el sistema sea **adaptativo**: en turnos con muchos contenedores llenos, el solver prioriza los urgentes y salta los casi vacíos. En turnos tranquilos, incluye contenedores opcionales de paso para vaciarlos preventivamente.

---

## 7. Modos de operación

Controlados por la variable `CUOPT_MODE`:

| Modo | Variable | Descripción |
|------|----------|-------------|
| `ortools` | `CUOPT_MODE=ortools` | Google OR-Tools, CPU puro. Default en desarrollo. No requiere API key ni GPU. |
| `api_catalog` | `CUOPT_MODE=api_catalog` + `CUOPT_API_KEY=...` | NVIDIA API Catalog (nube). Free tier: 5.000 req/mes. GPU compartida. Latencia variable (1-30s). |
| `self_hosted` | `CUOPT_MODE=self_hosted` + `CUOPT_SERVER_URL=http://...` | Servidor cuOpt propio en Docker o EC2 GPU. Sin límite de requests. Latencia determinista. |

**Decisión de selección de solver en `_make_solver()`:**

```python
def _make_solver():
    if _CUOPT_MODE in ("api_catalog", "self_hosted"):
        return CuOptSolver(mode=_CUOPT_MODE, ...)
    return ORToolsSolver()  # fallback para cualquier valor inválido también
```

La selección del solver es en tiempo de ejecución, no en deploy. Cambiar de OR-Tools a cuOpt solo requiere cambiar las variables de entorno de la Lambda — sin redeploy de código.

---

## 8. Polling y modelo asíncrono de la API

La API de NVIDIA cuOpt usa un modelo **asíncrono con polling**:

```
POST https://optimize.api.nvidia.com/v1/nvidia/cuopt
    → Si el problema es simple: responde 200 directamente
    → Si la GPU está ocupada:  responde 202 + header NVCF-REQID

Loop de polling (cada 2 segundos, timeout total 120s):
    GET https://optimize.api.nvidia.com/v1/status/{NVCF-REQID}
    → 202: sigue procesando, esperar
    → 200: solución lista, retornar JSON
    → 4xx/5xx: error, lanzar HTTPError
```

El timeout total de 120 segundos es generoso. En práctica, con instancias de ~100 nodos y `time_limit=10s`, cuOpt responde en 5-30 segundos dependiendo de la carga del cluster.

**Implementación en `_call_api_catalog()`:**

```python
deadline = time.monotonic() + _CUOPT_TIMEOUT_SECS  # 120s
while response.status_code == 202:
    request_id = response.headers.get("NVCF-REQID")
    if time.monotonic() > deadline:
        raise requests.exceptions.Timeout(...)
    time.sleep(_CUOPT_POLL_INTERVAL)  # 2s
    response = self._session.get(status_url, timeout=30)
```

El modo `self_hosted` es **síncrono**: el servidor responde directamente en el POST, sin polling. Esto lo hace más predecible para producción.

---

## 9. Fallback OR-Tools

OR-Tools es el backend por defecto para desarrollo local. Es más lento que cuOpt en instancias grandes pero suficiente para circuitos de Montevideo (~100 nodos, 1-2 camiones):

| Métrica | OR-Tools (CPU) | cuOpt (GPU) |
|---------|---------------|-------------|
| Instancia 100 nodos, 2 vehículos | ~2-8s | ~1-3s |
| Instancia 500 nodos, 5 vehículos | ~30-120s | ~3-10s |
| Requiere GPU | No | Sí |
| Requiere API key | No | Sí (api_catalog) |
| Calidad de solución | Buena (GLS) | Muy buena |

OR-Tools usa **Guided Local Search (GLS)** con `PATH_CHEAPEST_ARC` como solución inicial. Acepta los mismos parámetros que cuOpt, incluyendo prizes (via `AddDisjunction`).

**Mapeo de status de OR-Tools:**

```python
status_map = {
    7: "OPTIMAL",   # OR-Tools encontró óptimo probado
    1: "FEASIBLE",  # Solución encontrada, no probada óptima
    2: "FEASIBLE",  # Solución parcial (con timeout)
    4: "FEASIBLE",  # Timeout — retorna mejor solución encontrada
    3: "NO_SOLUTION",
    6: "INFEASIBLE",
}
```

---

## 10. Restricciones y limitaciones conocidas

### 10.1 No hay ventanas de tiempo por nodo

Actualmente `vehicle_time_windows` se configura con una ventana muy amplia `[0, time_limit × 10000]`. Las restricciones de turno (ej: no recoger antes de las 06:00) se aplican a nivel de Lambda antes de llamar al solver, no dentro del solver.

`get_time_window()` en `constraints.py` está implementado pero no se usa en `_build_payload()`. Para activarlo habría que agregar `task_time_windows` al payload de cuOpt.

### 10.2 No hay restricciones de tiempo de servicio

El tiempo de vaciado de cada contenedor (~3-5 minutos en la realidad) no se modela. La cost_matrix solo incluye tiempo de viaje entre puntos.

### 10.3 Un solo depósito por circuito

El handler asigna un depósito por circuito (Felipe Cardoso o Ruta 102) pero todos los camiones del circuito comparten el mismo depósito. No se soporta multi-depot heterogéneo dentro de un circuito.

### 10.4 Matriz de costos simétrica

OSRM puede generar matrices asimétricas (duración A→B ≠ B→A por sentidos únicos). Se usa `annotations=duration,distance` que ya captura asimetría. cuOpt y OR-Tools soportan matrices asimétricas sin problema.

### 10.5 Límite de 500 nodos por llamada OSRM

`_MAX_TABLE_SIZE = 500` en `osrm_client.py`. Los circuitos de Montevideo tienen ~100 contenedores, por lo que en la práctica esto no es una restricción activa. Si se escalara a circuitos más grandes habría que particionar la matriz.

### 10.6 Free tier de cuOpt: 5.000 requests/mes

Con ~117 circuitos activos × 4 turnos/día × 30 días = ~14.000 llamadas/mes. **El free tier no alcanza para producción completa.** Opciones:

- Usar OR-Tools para circuitos con pocos contenedores urgentes (< 10 nodos).
- Migrar a cuOpt self-hosted en EC2 `g4dn.xlarge` (~$0.50/h spot).
- Escalar gradualmente: solo activar cuOpt para circuitos con N > 50 contenedores.

### 10.7 Camiones virtuales

Si no hay camiones `idle` en DynamoDB para el circuito, el handler crea camiones virtuales con IDs `virtual-{circuit_id}-{v}`. Esto permite optimizar sin bloquear la ejecución, pero las rutas quedan sin un conductor real asignado.

---

## 11. Variables de entorno

| Variable | Valores | Default | Descripción |
|----------|---------|---------|-------------|
| `CUOPT_MODE` | `ortools`, `api_catalog`, `self_hosted` | `ortools` | Backend solver a usar |
| `CUOPT_API_KEY` | string | — | API key de NVIDIA (solo `api_catalog`) |
| `CUOPT_SERVER_URL` | URL | — | URL del servidor cuOpt (solo `self_hosted`) |
| `OSRM_URL` | URL | `http://localhost:5000` | URL del servidor OSRM |
| `OSRM_FALLBACK` | `haversine` | — | Fallback sin OSRM (desarrollo local) |
| `CONTAINERS_TABLE` | string | — | Nombre de la tabla DynamoDB de contenedores |
| `TRUCKS_TABLE` | string | — | Nombre de la tabla DynamoDB de camiones |
| `ROUTES_TABLE` | string | — | Nombre de la tabla DynamoDB de rutas |

---

## 12. Testing

### Test funcional básico

```bash
# OR-Tools (no requiere ninguna configuración)
python cuopt-client/test_solver.py

# cuOpt API Catalog (requiere CUOPT_API_KEY en .env o env var)
python cuopt-client/test_solver.py --cuopt

# cuOpt self-hosted
python cuopt-client/test_solver.py --cuopt --mode self_hosted --server http://localhost:8080

# Cambiar time limit
python cuopt-client/test_solver.py --cuopt --time-limit 30
```

El test crea un problema con 10 nodos (1 depósito + 9 contenedores en Montevideo), 2 vehículos de 2.500 kg, y valida automáticamente:

- Status es `OPTIMAL` o `FEASIBLE`
- Al menos 1 ruta activa encontrada
- Costo total > 0
- Cada vehículo respeta su capacidad
- Cada ruta empieza y termina en el depósito

### Test de OSRM

```bash
python cuopt-client/osrm_client.py
```

Genera matrices de duración y distancia entre 5 puntos de Montevideo y una ruta Centro → Cerro → Felipe Cardoso.

### Invocar la Lambda directamente

```bash
# Optimizar un circuito específico
aws lambda invoke \
  --function-name smartwaste-dev-route-optimizer \
  --region us-east-1 \
  --payload '{"circuit_id":"A_DU_RM_CL_103"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/out.json && cat /tmp/out.json

# Ver logs en tiempo real
aws logs tail /aws/lambda/smartwaste-dev-route-optimizer \
  --since 5m --region us-east-1 --follow
```
