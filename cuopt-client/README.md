# cuopt-client — SmartWaste MVD

Cliente VRP para SmartWaste MVD. Resuelve el **Capacitated Vehicle Routing Problem (CVRP)** de recolección de residuos en Montevideo usando NVIDIA cuOpt como solver primario y Google OR-Tools como fallback CPU.

---

## Contenido del módulo

```
cuopt-client/
  vrp_solver.py    — CuOptSolver + ORToolsSolver (misma interfaz)
  osrm_client.py   — Matriz de distancias desde OSRM (calles reales)
  constraints.py   — Demanda en kg, ventanas de tiempo por turno
  test_solver.py   — Test funcional con datos sintéticos de MVD
  .env             — CUOPT_API_KEY para desarrollo local
```

---

## ¿Qué es NVIDIA cuOpt?

[NVIDIA cuOpt](https://docs.nvidia.com/cuopt/user-guide/latest/introduction.html) es un solver de optimización acelerado por GPU. Puede resolver problemas de tipo LP, QP, MILP y, lo que usamos aquí, **Vehicle Routing Problems (VRP)** con restricciones de capacidad, ventanas de tiempo y múltiples depósitos.

A diferencia de solvers CPU como OR-Tools, cuOpt corre en GPU (CUDA), lo que le permite explorar el espacio de soluciones usando heurísticas paralelas. Para un circuito típico de Montevideo (~100 contenedores, 1-3 camiones), cuOpt encuentra una solución óptima o casi-óptima en **< 2 segundos**.

### Modos de acceso disponibles

| Modo | Endpoint | Costo | Uso en este proyecto |
|------|----------|-------|----------------------|
| `api_catalog` | `https://optimize.api.nvidia.com/v1/nvidia/cuopt` | Free tier: 5 000 req/mes | **Producción actual** |
| `self_hosted` | EC2 GPU / Docker local | Instancia GPU + almacenamiento | Migración futura |
| `ortools` | — (librería local) | Sin costo | Fallback dev sin GPU |

---

## El problema que resolvemos: CVRP

Dado un circuito de recolección con:
- Un **depósito** (punto de inicio y fin: Felipe Cardoso o Ruta 102)
- N **contenedores** con distintos niveles de llenado
- K **camiones** con capacidad máxima en kg

Queremos encontrar **K rutas** (una por camión) que:
1. Partan del depósito
2. Visiten todos los contenedores que necesitan recolección
3. Respeten la capacidad del camión (no superar `capacity_kg`)
4. Minimicen el tiempo total de recorrido
5. Vuelvan al depósito

Este es un problema NP-duro. cuOpt lo resuelve mediante heurísticas GPU en el tiempo dado por `time_limit`.

---

## Flujo de datos completo

```
DynamoDB (containers)
    │
    ▼
handler.py  →  _get_containers(circuit_id)
                    │
                    ▼
             _build_problem()          ← constraints.py: estimate_demand_kg()
             locations = [depot] + [contenedores] + [depot]
             demands   = [0]     + [kg_por_cont]  + [0]
                    │
                    ▼
             OSRMClient.get_distance_matrix(locations)
                    │  GET /table/v1/driving/{coords}?annotations=duration,distance
                    ▼
             cost_matrix  (N×N, enteros, segundos)
             dist_matrix  (N×N, float, metros)
                    │
                    ▼
             CuOptSolver.solve_vrp(
                 cost_matrix, num_vehicles, demands,
                 capacities, depot_start_idx, depot_end_idx, time_limit
             )
                    │  POST https://optimize.api.nvidia.com/v1/nvidia/cuopt
                    ▼
             vrp_result = {
                 "routes":     {0: [0, 5, 3, 12, 0], 1: [0, 8, 2, 0]},
                 "total_cost": 4320,       ← segundos totales
                 "status":     "OPTIMAL",
                 "solver":     "cuopt"
             }
                    │
                    ▼
             _save_route()  →  DynamoDB (routes)
             notify_drivers()  →  WebSocket  →  Driver App
```

---

## Construcción del payload cuOpt

El payload que enviamos a la API tiene esta estructura (definida en `vrp_solver.py:_build_payload()`):

```python
{
  "action": "cuOpt_OptimizedRouting",
  "data": {
    # Matriz de costos: la API soporta múltiples tipos de vehículo,
    # cada uno con su propia matriz. Usamos un solo tipo ("1").
    "cost_matrix_data": {
      "data": {
        "1": [[0, 120, 85, ...],   # cost_matrix[i][j] = tiempo en segundos
              [120, 0, 45, ...],    # de nodo i a nodo j (OSRM Table API)
              ...]
      }
    },
    "cost_waypoint_graph_data": null,
    "travel_time_matrix_data":  null,
    "travel_time_waypoint_graph_data": null,

    # Datos de la flota
    "fleet_data": {
      # Cada vehículo sale del depot (índice 0) y vuelve al depot (índice N-1)
      # depot_start y depot_end son el mismo nodo físico pero cuOpt los trata
      # como nodos lógicos distintos en el grafo.
      "vehicle_locations": [[0, N-1], [0, N-1], ...],  # [start, end] por camión

      "vehicle_ids": ["veh-0", "veh-1", ...],           # strings requeridos por la API

      # capacities[dimensión][vehículo]
      # Dimensión 0 = kg. Para restricciones multi-dimensión (kg + volumen)
      # se añadirían más filas. Hoy usamos solo kg.
      "capacities": [[25000, 25000, ...]],              # kg por camión

      # Ventana de tiempo amplia (no queremos restricciones de horario aquí;
      # el filtro por turno se aplica antes en _get_circuits_for_shift).
      "vehicle_time_windows": [[0, 50000], [0, 50000], ...],

      # vehicle_types mapea cada camión a la matriz de costos que usa.
      # Todos usan el tipo "1" (única matriz definida arriba).
      "vehicle_types": [1, 1, ...]
    },

    # Datos de las tareas (contenedores a visitar)
    # Los nodos de depósito (0 y N-1) NO se incluyen aquí.
    "task_data": {
      "task_locations": [1, 2, 3, ..., N-2],   # índices de contenedores en cost_matrix
      "task_ids": ["task-1", "task-2", ...],    # strings requeridos por la API

      # demand[dimensión][tarea]
      # Misma convención que capacities: demand[0][i] = kg del contenedor i
      "demand": [[180, 432, 648, ...]]          # kg estimados por contenedor
    },

    "solver_config": {
      "time_limit": 10.0,          # segundos de cómputo máximo en GPU
      "objectives": {
        "cost":                       1,   # minimizar costo (tiempo de viaje)
        "travel_time":                0,
        "variance_route_size":        0,   # si 1: equilibrar paradas entre camiones
        "variance_route_service_time":0,
        "prize":                      0
      },
      "verbose_mode":  false,
      "error_logging": true
    }
  },
  "client_version": ""
}
```

### Por qué `depot_start_idx != depot_end_idx`

cuOpt modela el depósito como **dos nodos lógicos distintos** aunque sean el mismo punto geográfico:
- `depot_start_idx = 0` — todos los camiones salen de aquí
- `depot_end_idx = N-1` — todos los camiones terminan aquí

Esto permite a cuOpt representar grafos asimétricos (el camino de vuelta puede ser diferente al de ida). En nuestro caso ambos apuntan a las mismas coordenadas del depósito (Felipe Cardoso o Ruta 102).

### Indexación de nodos

```
Índice 0     → depot_start  (Felipe Cardoso o Ruta 102)
Índice 1     → contenedor[0]
Índice 2     → contenedor[1]
...
Índice N-2   → contenedor[N-3]
Índice N-1   → depot_end    (mismo lugar que índice 0)
```

La `cost_matrix` es de tamaño `N × N` donde `N = len(contenedores) + 2`.

---

## Respuesta de cuOpt y parseo

La API devuelve (en `_parse_response()`):

```json
{
  "response": {
    "solver_response": {
      "status": 0,
      "solution_cost": 4320.5,
      "vehicle_data": {
        "veh-0": {
          "route":         [0, 5, 3, 12, 14],
          "task_id":       ["task-5", "task-3", "task-12", "task-14"],
          "arrival_stamp": [0, 185.3, 340.1, 520.8, 710.2]
        },
        "veh-1": {
          "route":         [0, 8, 2, 14],
          "task_id":       ["task-8", "task-2"],
          "arrival_stamp": [0, 210.5, 390.2, 580.0]
        }
      }
    }
  }
}
```

#### Mapeo de status numérico

| `status` | String interno | Significado |
|----------|----------------|-------------|
| `0` | `"OPTIMAL"` | Solución óptima o mejor encontrada en `time_limit` |
| `1` | `"FEASIBLE"` | Solución factible pero no necesariamente óptima |
| `< 0` | `"INFEASIBLE"` | El problema no tiene solución (ej: demanda > capacidad total) |

#### Polling (HTTP 202)

La API de NVIDIA puede devolver **HTTP 202** si la GPU está ocupada procesando otro problema. En ese caso la respuesta incluye el header `NVCF-REQID` con un ID de request. `_call_api_catalog()` hace polling automático contra `https://optimize.api.nvidia.com/v1/status/{request_id}` hasta obtener HTTP 200 o agotar `_CUOPT_TIMEOUT_SECS = 120s`.

```
POST /v1/nvidia/cuopt  →  HTTP 202  (en cola)
                                │
                          GET /v1/status/{id}  →  HTTP 202 (procesando)
                                │  (espera 2s)
                          GET /v1/status/{id}  →  HTTP 200 (listo)
```

---

## Cálculo de demanda (`constraints.py`)

```python
def estimate_demand_kg(fill_level: float, capacity_liters: float = 2400) -> float:
    density = 0.30  # kg/L — residuos domésticos mixtos en Montevideo
    return (fill_level / 100.0) * capacity_liters * density
```

Un contenedor estándar de MVD (2400 L) al 100% pesa **~720 kg**.
Un camión de 25 t puede vaciar **~34 contenedores llenos** en un turno.

### Ventanas de tiempo por turno

| Turno | Código | Horario |
|-------|--------|---------|
| Mañana | `morning` / `M` | 06:00 – 14:00 |
| Tarde | `afternoon` / `V` | 14:00 – 22:00 |
| Noche | `night` / `N` | 22:00 – 06:00 (+1d) |

El turno nocturno usa `latest = 30 * 3600` (30 h) para representar el cruce de medianoche sin aritmética modular en el solver.

---

## Matriz de distancias: OSRM (`osrm_client.py`)

cuOpt necesita una matriz de costos `N×N`. En lugar de distancia euclidea usamos **tiempos de conducción reales** calculados por OSRM (Open Source Routing Machine) con datos de OpenStreetMap para Uruguay.

```python
# Llamada interna que hace OSRMClient
GET /table/v1/driving/lon1,lat1;lon2,lat2;...?annotations=duration,distance

# Respuesta
{
  "durations": [[0, 120, 85, ...], ...],  # segundos N×N
  "distances": [[0, 980, 720, ...], ...]  # metros N×N
}
```

La **cost_matrix que le pasamos a cuOpt son las duraciones en segundos** (enteros). cuOpt minimiza el tiempo total de conducción.

### Fallback haversine

Si OSRM no está disponible (`OSRM_FALLBACK=haversine`), `osrm_client.py` genera una matriz sintética:

```python
distancia_haversine × 1.35   # factor de detour urbano
tiempo = distancia / (30 km/h en m/s)
```

Esto es suficiente para desarrollo local pero produce rutas subóptimas vs. distancias reales de calles.

---

## OR-Tools: fallback CPU

Cuando `CUOPT_MODE=ortools`, `ORToolsSolver` usa `ortools.constraint_solver.routing`:

- Estrategia inicial: `PATH_CHEAPEST_ARC`
- Metaheurística: `GUIDED_LOCAL_SEARCH`
- Time limit configurable (default: 5s)

OR-Tools es 10–100x más lento que cuOpt para circuitos grandes (> 50 nodos) pero no requiere GPU ni API key. Útil para desarrollo local sin conexión a NVIDIA.

---

## Cómo probar localmente

```bash
cd cuopt-client

# Test con OR-Tools (sin API key, sin GPU)
python3 test_solver.py

# Test con cuOpt API Catalog (requiere .env con CUOPT_API_KEY)
python3 test_solver.py --cuopt

# Test con cuOpt self-hosted
python3 test_solver.py --cuopt --mode self_hosted --server http://localhost:8080

# Ajustar time limit
python3 test_solver.py --cuopt --time-limit 15
```

El test resuelve un problema sintético de 10 nodos (1 depósito + 9 contenedores reales de MVD) con 2 camiones de 2500 kg. Valida que:
- El status sea `OPTIMAL` o `FEASIBLE`
- Al menos 1 ruta esté activa
- El costo total sea > 0
- Cada camión respete su capacidad
- Cada ruta empiece y termine en el depósito

---

## Variables de entorno

| Variable | Descripción | Default |
|----------|-------------|---------|
| `CUOPT_MODE` | `ortools` \| `api_catalog` \| `self_hosted` | `ortools` |
| `CUOPT_API_KEY` | API key de NVIDIA API Catalog | — |
| `CUOPT_SERVER_URL` | URL cuOpt self-hosted | — |
| `OSRM_FALLBACK` | `haversine` para desarrollo sin OSRM | — |

Para desarrollo local, crear `cuopt-client/.env`:

```
CUOPT_API_KEY=nvapi-...
```

---

## Diagrama de clases

```
vrp_solver.py
├── ORToolsSolver
│     └── solve_vrp() → dict
└── CuOptSolver
      ├── __init__(mode, api_key, server_url)
      ├── solve_vrp() → dict
      ├── _build_payload()      ← construye JSON para API
      ├── _call_api_catalog()   ← POST + polling HTTP 202
      ├── _call_self_hosted()   ← POST síncrono
      └── _parse_response()     ← normaliza vehicle_data

osrm_client.py
└── OSRMClient
      ├── get_distance_matrix(locations) → {durations, distances}
      ├── get_route(waypoints)           → {distance_m, duration_s, geometry}
      └── _haversine_matrix()            ← fallback sin OSRM

constraints.py
├── estimate_demand_kg(fill_level, capacity_liters)
└── get_time_window(shift)
```

---

## Referencias

- [NVIDIA cuOpt — User Guide](https://docs.nvidia.com/cuopt/user-guide/latest/introduction.html)
- [cuOpt Examples (GitHub)](https://github.com/NVIDIA/cuopt-examples)
- [OSRM Table API](http://project-osrm.org/docs/v5.5.1/api/#table-service)
- [OR-Tools VRP](https://developers.google.com/optimization/routing/vrp)
