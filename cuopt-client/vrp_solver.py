"""
vrp_solver.py — SmartWaste MVD

Resuelve el Problema de Ruteo de Vehículos (VRP) con restricciones de
capacidad (CVRP).  Dos backends intercambiables con la misma interfaz:

  CuOptSolver   — NVIDIA cuOpt (GPU). Dos modos:
                    "api_catalog"  → https://optimize.api.nvidia.com  (free tier)
                    "self_hosted"  → servidor cuOpt local en Docker/EC2

  ORToolsSolver — Google OR-Tools (CPU). Fallback sin GPU, sin API key.

Interfaz común:

    solver.solve_vrp(
        cost_matrix      = [[0, 10, ...], ...],   # N×N, enteros (segundos o metros)
        num_vehicles     = 3,
        demands          = [0, 12, 8, ...],        # kg por nodo (0 en depósito)
        capacities       = [720, 720, 720],        # kg máximos por vehículo
        depot_start_idx  = 0,
        depot_end_idx    = 0,
        time_limit       = 5,
    )
    →  {
          "routes":     {0: [0, 3, 7, 0], 1: [0, 5, 2, 0], ...},
          "total_cost": 1234,
          "status":     "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | ...,
          "solver":     "cuopt" | "ortools",
       }
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# Carga del .env (solo en desarrollo local)
# ─────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    """
    Carga variables de entorno desde .env en el mismo directorio que este archivo.
    Solo activo si el archivo existe; silencioso si no (Lambda usa env vars reales).
    Intenta usar python-dotenv; si no está instalado, parsea el archivo manualmente.
    """
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)  # no sobreescribir vars ya seteadas
        logger.debug("Env vars cargadas desde %s (python-dotenv)", env_path)
        return
    except ImportError:
        pass

    # Fallback manual: parsea KEY=VALUE, ignora comentarios y líneas vacías
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    logger.debug("Env vars cargadas desde %s (parser manual)", env_path)


_load_dotenv()


# ─────────────────────────────────────────────────────────
# Utilidades compartidas
# ─────────────────────────────────────────────────────────

def _validate_inputs(
    cost_matrix: list[list[int]],
    num_vehicles: int,
    demands: list[float],
    capacities: list[float],
    depot_start_idx: int,
    depot_end_idx: int,
) -> None:
    n = len(cost_matrix)
    if any(len(row) != n for row in cost_matrix):
        raise ValueError("cost_matrix debe ser cuadrada (N×N)")
    if len(demands) != n:
        raise ValueError(f"demands debe tener {n} elementos (uno por nodo)")
    if len(capacities) != num_vehicles:
        raise ValueError(f"capacities debe tener {num_vehicles} elementos")
    if not (0 <= depot_start_idx < n):
        raise ValueError(f"depot_start_idx={depot_start_idx} fuera de rango [0, {n})")
    if not (0 <= depot_end_idx < n):
        raise ValueError(f"depot_end_idx={depot_end_idx} fuera de rango [0, {n})")


# ─────────────────────────────────────────────────────────
# OR-Tools fallback (CPU, sin GPU, sin API key)
# ─────────────────────────────────────────────────────────

class ORToolsSolver:
    """
    Resuelve CVRP usando Google OR-Tools (ortools.constraint_solver.routing).

    Ideal para desarrollo local y entornos sin GPU.
    Para instalar: pip install ortools

    En producción se prefiere CuOptSolver (10-100× más rápido para instancias
    grandes gracias a la GPU), pero ORToolsSolver es suficiente para circuitos
    de hasta ~150 nodos con tiempos de respuesta < 10 s.
    """

    def solve_vrp(
        self,
        cost_matrix: list[list[int]],
        num_vehicles: int,
        demands: list[float],
        capacities: list[float],
        depot_start_idx: int = 0,
        depot_end_idx: int = 0,
        time_limit: int = 5,
        prizes: list[float] | None = None,
    ) -> dict[str, Any]:
        try:
            from ortools.constraint_solver import pywrapcp, routing_enums_pb2
        except ImportError as exc:
            raise ImportError(
                "ortools no está instalado. Instalarlo con: pip install ortools"
            ) from exc

        _validate_inputs(cost_matrix, num_vehicles, demands, capacities,
                         depot_start_idx, depot_end_idx)

        n       = len(cost_matrix)
        cap_int = [int(c) for c in capacities]
        dem_int = [int(d) for d in demands]

        manager = pywrapcp.RoutingIndexManager(n, num_vehicles, depot_start_idx)
        routing = pywrapcp.RoutingModel(manager)

        def cost_callback(from_idx: int, to_idx: int) -> int:
            return cost_matrix[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

        transit_cb = routing.RegisterTransitCallback(cost_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

        def demand_callback(idx: int) -> int:
            return dem_int[manager.IndexToNode(idx)]

        demand_cb = routing.RegisterUnaryTransitCallback(demand_callback)
        routing.AddDimensionWithVehicleCapacity(
            demand_cb, 0, cap_int, True, "Capacity",
        )

        # Paradas opcionales: AddDisjunction con penalty = prize del nodo.
        # Nodos con prize alto son prácticamente obligatorios; nodos con
        # prize bajo se visitan solo si el desvío es menor que el prize.
        depot_set = {depot_start_idx, depot_end_idx}
        if prizes is not None:
            for node in range(n):
                if node in depot_set:
                    continue
                penalty = int(prizes[node])
                routing.AddDisjunction(
                    [manager.NodeToIndex(node)],
                    penalty,
                )

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        params.time_limit.seconds = time_limit
        params.log_search = False

        logger.info("OR-Tools: n=%d vehículos=%d time_limit=%ds", n, num_vehicles, time_limit)
        solution = routing.SolveWithParameters(params)

        if solution is None:
            logger.warning("OR-Tools: no encontró solución")
            return {"routes": {}, "total_cost": 0, "status": "NO_SOLUTION", "solver": "ortools"}

        routes: dict[int, list[int]] = {}
        for v in range(num_vehicles):
            idx   = routing.Start(v)
            route: list[int] = []
            while not routing.IsEnd(idx):
                route.append(manager.IndexToNode(idx))
                idx = solution.Value(routing.NextVar(idx))
            route.append(manager.IndexToNode(idx))
            if len(route) > 2 or (len(route) == 2 and route[0] != route[1]):
                routes[v] = route

        # routing.status() int: 0=NOT_SOLVED, 1=SUCCESS, 2=PARTIAL, 3=FAIL,
        #                       4=FAIL_TIMEOUT, 5=INVALID, 6=INFEASIBLE, 7=OPTIMAL
        status_map = {7: "OPTIMAL", 1: "FEASIBLE", 2: "FEASIBLE", 4: "FEASIBLE",
                      3: "NO_SOLUTION", 6: "INFEASIBLE"}
        status = status_map.get(routing.status(), "FEASIBLE")

        logger.info("OR-Tools: status=%s cost=%d rutas=%d", status, solution.ObjectiveValue(), len(routes))
        return {
            "routes":     routes,
            "total_cost": solution.ObjectiveValue(),
            "status":     status,
            "solver":     "ortools",
        }


# ─────────────────────────────────────────────────────────
# NVIDIA cuOpt
# ─────────────────────────────────────────────────────────

# Endpoints oficiales (https://docs.nvidia.com/cuopt/)
_CUOPT_INVOKE_URL = "https://optimize.api.nvidia.com/v1/nvidia/cuopt"
_CUOPT_STATUS_URL = "https://optimize.api.nvidia.com/v1/status/{request_id}"

# Tiempo máximo de espera total para una respuesta (la GPU puede estar fría)
_CUOPT_TIMEOUT_SECS   = 120
# Intervalo entre polls cuando la API devuelve 202 (procesando)
_CUOPT_POLL_INTERVAL  = 2.0


class CuOptSolver:
    """
    Resuelve CVRP usando NVIDIA cuOpt vía la API oficial de optimización.

    Dos modos:

    "api_catalog":
        Usa https://optimize.api.nvidia.com (free tier: 5 000 req/mes).
        Lee CUOPT_API_KEY del entorno o del .env en el mismo directorio.

    "self_hosted":
        Usa un servidor cuOpt local (Docker/EC2 GPU).
        Requiere server_url apuntando al endpoint HTTP de cuOpt.

    Args:
        mode:       "api_catalog" (default) o "self_hosted"
        api_key:    API key de NVIDIA. Si es None, usa os.environ["CUOPT_API_KEY"].
        server_url: URL del servidor cuOpt self-hosted (solo para self_hosted).
    """

    def __init__(
        self,
        mode: str = "api_catalog",
        api_key: str | None = None,
        server_url: str | None = None,
    ) -> None:
        if mode not in ("api_catalog", "self_hosted"):
            raise ValueError(f"mode debe ser 'api_catalog' o 'self_hosted', no {mode!r}")

        self._mode    = mode
        self._session = requests.Session()

        if mode == "api_catalog":
            self._api_key = api_key or os.environ.get("CUOPT_API_KEY")
            if not self._api_key:
                raise ValueError(
                    "CuOptSolver(mode='api_catalog') requiere api_key o "
                    "la variable de entorno CUOPT_API_KEY (o un .env en cuopt-client/)."
                )
            self._session.headers.update({
                "Authorization": f"Bearer {self._api_key}",
                "Accept":        "application/json",
                "Content-Type":  "application/json",
            })

        elif mode == "self_hosted":
            if not server_url:
                raise ValueError(
                    "CuOptSolver(mode='self_hosted') requiere server_url. "
                    "Ej: server_url='http://localhost:8080'"
                )
            self._server_url = server_url.rstrip("/")
            self._session.headers.update({
                "Accept":       "application/json",
                "Content-Type": "application/json",
            })

    # ── API pública ───────────────────────────────────────────

    def solve_vrp(
        self,
        cost_matrix: list[list[int]],
        num_vehicles: int,
        demands: list[float],
        capacities: list[float],
        depot_start_idx: int = 0,
        depot_end_idx: int = 0,
        time_limit: int = 5,
        prizes: list[float] | None = None,
    ) -> dict[str, Any]:
        """
        Resuelve el problema CVRP con NVIDIA cuOpt.

        Mismos argumentos y formato de retorno que ORToolsSolver.solve_vrp().

        Raises:
            requests.exceptions.Timeout:   cuOpt no respondió en _CUOPT_TIMEOUT_SECS.
            requests.exceptions.HTTPError: Error HTTP del servidor/API.
            RuntimeError:                  cuOpt devolvió error en el payload.
            ValueError:                    Inputs inválidos.
        """
        _validate_inputs(cost_matrix, num_vehicles, demands, capacities,
                         depot_start_idx, depot_end_idx)

        payload = self._build_payload(
            cost_matrix, num_vehicles, demands, capacities,
            depot_start_idx, depot_end_idx, time_limit, prizes,
        )

        if self._mode == "api_catalog":
            raw = self._call_api_catalog(payload)
        else:
            raw = self._call_self_hosted(payload)

        return self._parse_response(raw)

    # ── Construcción del payload ──────────────────────────────

    def _build_payload(
        self,
        cost_matrix: list[list[int]],
        num_vehicles: int,
        demands: list[float],
        capacities: list[float],
        depot_start_idx: int,
        depot_end_idx: int,
        time_limit: int,
        prizes: list[float] | None = None,
    ) -> dict:
        """
        Construye el JSON según la especificación oficial de cuOpt.

        Estructura: {"action": "cuOpt_OptimizedRouting", "data": {...}}

        Notas sobre dimensiones:
          capacities → lista de listas: capacities[dim][v]. Para una sola
                       dimensión (kg): [[cap_v0, cap_v1, ...]]
          demand     → lista de listas: demand[dim][task]. Para una sola
                       dimensión: [[dem_t0, dem_t1, ...]]

          La API valida que len(capacities[dim]) == num_vehicles y que
          len(demand[dim]) == num_tasks para cada dimensión.
        """
        n = len(cost_matrix)

        # Nodos de parada (todo excepto los depósitos)
        depot_set   = {depot_start_idx, depot_end_idx}
        task_indices = [i for i in range(n) if i not in depot_set]
        task_ids     = [f"task-{i}" for i in task_indices]

        # IDs de vehículos como strings (requisito de la API)
        vehicle_ids = [f"veh-{v}" for v in range(num_vehicles)]

        # Ventana de tiempo amplia: no queremos restricciones de tiempo
        # en esta etapa (la ventana real se aplica por turno, no por nodo).
        max_time = time_limit * 10_000

        data: dict = {
            "cost_waypoint_graph_data":        None,
            "travel_time_waypoint_graph_data": None,
            # La API requiere al menos una matriz de costo.
            # Key "1" = tipo de vehículo 1 (todos los vehículos usan el mismo grafo).
            "cost_matrix_data": {
                "data": {"1": cost_matrix}
            },
            "travel_time_matrix_data": None,
            "fleet_data": {
                "vehicle_locations":     [[depot_start_idx, depot_end_idx]] * num_vehicles,
                "vehicle_ids":           vehicle_ids,
                # capacities[dim][v] — una sola dimensión: [[cap_v0, cap_v1, ...]]
                "capacities":            [[int(c) for c in capacities]],
                "vehicle_time_windows":  [[0, max_time]] * num_vehicles,
                # vehicle_types indica qué matriz de costos usa cada vehículo.
                # Todos usan el tipo 1 (único tipo definido en cost_matrix_data).
                # Requerido por la API cuando la key del mapa de matrices no es 0.
                "vehicle_types":         [1] * num_vehicles,
            },
            "task_data": {
                "task_locations": task_indices,
                "task_ids":       task_ids,
                # demand[dim][task] — una sola dimensión: [[dem_t0, dem_t1, ...]]
                "demand": [[int(demands[i]) for i in task_indices]],
                # prizes: valor por tarea — el solver las visita si el premio
                # justifica el desvío. Sin prizes → todas son obligatorias.
                **({"prizes": [int(prizes[i]) for i in task_indices]} if prizes else {}),
            },
            "solver_config": {
                "time_limit":  float(time_limit),
                "objectives": {
                    "cost":                      1,
                    "travel_time":               0,
                    "variance_route_size":        0,
                    "variance_route_service_time": 0,
                    "prize":                     1 if prizes else 0,
                },
                "verbose_mode": False,
                "error_logging": True,
            },
        }

        return {
            "action":         "cuOpt_OptimizedRouting",
            "data":           data,
            "client_version": "",
        }

    # ── Llamadas HTTP ─────────────────────────────────────────

    def _call_api_catalog(self, payload: dict) -> dict:
        """
        POST al endpoint oficial de NVIDIA cuOpt.

        La API puede devolver 202 (procesando) mientras la GPU resuelve el
        problema.  En ese caso se hace polling hasta obtener 200 o un error,
        con un timeout total de _CUOPT_TIMEOUT_SECS segundos.
        """
        logger.info("cuOpt API: POST %s", _CUOPT_INVOKE_URL)

        try:
            response = self._session.post(
                _CUOPT_INVOKE_URL,
                json=payload,
                timeout=_CUOPT_TIMEOUT_SECS,
            )
        except requests.exceptions.Timeout:
            logger.error("cuOpt API: timeout en POST inicial (%ds)", _CUOPT_TIMEOUT_SECS)
            raise

        # Polling mientras la API procesa (HTTP 202 = en cola / ejecutando)
        deadline = time.monotonic() + _CUOPT_TIMEOUT_SECS
        while response.status_code == 202:
            request_id = response.headers.get("NVCF-REQID")
            if not request_id:
                raise RuntimeError("cuOpt devolvió 202 sin header NVCF-REQID")

            if time.monotonic() > deadline:
                raise requests.exceptions.Timeout(
                    f"cuOpt no completó el problema en {_CUOPT_TIMEOUT_SECS}s "
                    f"(request_id={request_id})"
                )

            logger.debug("cuOpt API: 202 procesando — polling request_id=%s", request_id)
            time.sleep(_CUOPT_POLL_INTERVAL)

            fetch_url = _CUOPT_STATUS_URL.format(request_id=request_id)
            try:
                response = self._session.get(fetch_url, timeout=30)
            except requests.exceptions.Timeout:
                logger.warning("cuOpt API: timeout en poll de status, reintentando...")
                continue

        # Cualquier código >= 400 lanza HTTPError
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            logger.error(
                "cuOpt API: HTTP %s — %s",
                exc.response.status_code,
                exc.response.text[:400],
            )
            raise

        logger.info("cuOpt API: respuesta recibida (HTTP %s)", response.status_code)
        return response.json()

    def _call_self_hosted(self, payload: dict) -> dict:
        """Llama al servidor cuOpt self-hosted. No requiere polling (síncrono)."""
        url = f"{self._server_url}/cuopt/"
        logger.info("cuOpt self-hosted: POST %s", url)

        try:
            response = self._session.post(url, json=payload, timeout=_CUOPT_TIMEOUT_SECS)
            response.raise_for_status()
        except requests.exceptions.Timeout:
            logger.error("cuOpt self-hosted: timeout (%ds)", _CUOPT_TIMEOUT_SECS)
            raise
        except requests.exceptions.HTTPError as exc:
            logger.error(
                "cuOpt self-hosted: HTTP %s — %s",
                exc.response.status_code,
                exc.response.text[:400],
            )
            raise

        return response.json()

    # ── Parseo de respuesta ───────────────────────────────────

    def _parse_response(self, data: dict) -> dict[str, Any]:
        """
        Normaliza la respuesta de cuOpt al formato común del proyecto.

        Estructura esperada de la respuesta:
        {
            "response": {
                "solver_response": {
                    "status": 0,          # 0=SUCCESS, 1=PARTIAL, <0=ERROR
                    "solution_cost": 123,
                    "vehicle_data": {
                        "veh-0": {
                            "task_id":      ["task-3", "task-1"],
                            "route":        [0, 3, 1, 0],   # node indices
                            "arrival_stamp": [0, 5.2, 8.1, 12.3]
                        },
                        ...
                    }
                }
            }
        }
        """
        # La respuesta puede venir envuelta en "response" (API Catalog)
        # o directamente en el payload (self-hosted).
        result = data.get("response", data)

        if "error" in result:
            raise RuntimeError(f"cuOpt error en la respuesta: {result['error']}")

        solver_resp  = result.get("solver_response", {})
        vehicle_data = solver_resp.get("vehicle_data", {})
        raw_status   = solver_resp.get("status", -1)
        total_cost   = solver_resp.get("solution_cost", 0)

        # Mapeo de status numérico → string
        # 0 = éxito (óptimo o factible), 1 = éxito parcial, <0 = error
        if raw_status == 0:
            status_str = "OPTIMAL"
        elif raw_status == 1:
            status_str = "FEASIBLE"
        elif raw_status < 0:
            status_str = "INFEASIBLE"
            logger.warning("cuOpt devolvió status=%d (error/infactible)", raw_status)
        else:
            status_str = f"UNKNOWN({raw_status})"

        # Construir rutas: cada vehículo → lista de node_indices con depósitos
        routes: dict[int, list[int]] = {}
        for v_offset, (v_id, v_info) in enumerate(vehicle_data.items()):
            # "route" contiene los node indices incluyendo depósitos
            route = v_info.get("route")
            if route is not None and len(route) > 2:
                routes[v_offset] = [int(x) for x in route]
            else:
                # fallback: reconstruir desde task_id (strings "task-N" → int N)
                task_id_strs = v_info.get("task_id", [])
                if task_id_strs:
                    task_nodes = [int(t.split("-")[1]) for t in task_id_strs]
                    routes[v_offset] = task_nodes

        logger.info(
            "cuOpt: status=%s cost=%s rutas_activas=%d",
            status_str, total_cost, len(routes),
        )

        return {
            "routes":     routes,
            "total_cost": float(total_cost),
            "status":     status_str,
            "solver":     "cuopt",
        }
