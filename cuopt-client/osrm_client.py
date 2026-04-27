"""
osrm_client.py — SmartWaste MVD

Cliente HTTP para el servidor OSRM auto-hospedado.
Abstrae la API de OSRM y resuelve la inversión de coordenadas:
el proyecto usa (latitud, longitud) pero OSRM espera (longitud, latitud).

Uso típico desde el route-optimizer:

    from cuopt_client.osrm_client import OSRMClient

    client = OSRMClient()
    result = client.get_distance_matrix(locations)
    durations = result["durations"]   # matriz N×N en segundos
    distances = result["distances"]   # matriz N×N en metros
"""

import logging
import math
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Máximo de locations por llamada a la Table API de OSRM.
# El servidor está configurado con --max-table-size 10000,
# pero matrices muy grandes consumen mucha RAM. 500 es un límite
# conservador que cubre cualquier circuito de Montevideo (~100 contenedores)
# con margen amplio para el depósito + camiones.
_MAX_TABLE_SIZE = 500

_DEFAULT_TIMEOUT = 30.0  # segundos


class OSRMError(Exception):
    """Error devuelto por la API de OSRM (code != 'Ok')."""


# ─────────────────────────────────────────────────────────
# Fallback haversine (sin OSRM)
# ─────────────────────────────────────────────────────────

_URBAN_SPEED_MS = 30_000 / 3600  # 30 km/h en m/s — velocidad media urbana Montevideo
_DETOUR_FACTOR  = 1.35            # calles no son línea recta; factor empírico


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia en metros entre dos puntos (fórmula haversine)."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _haversine_matrix(
    locations: list[tuple[float, float]],
) -> dict[str, Any]:
    """
    Genera matrices de duración y distancia usando haversine + factor de detour.
    Usado como fallback cuando OSRM no está disponible (desarrollo/testing).
    """
    n = len(locations)
    distances = [[0.0] * n for _ in range(n)]
    durations = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            d = _haversine_m(*locations[i], *locations[j]) * _DETOUR_FACTOR
            distances[i][j] = d
            durations[i][j] = d / _URBAN_SPEED_MS
    return {"durations": durations, "distances": distances, "locations": locations, "n": n}


class OSRMClient:
    """
    Cliente para la API HTTP de OSRM.

    Métodos principales:
        get_distance_matrix(locations) → dict con matrices duration/distance
        get_route(waypoints)           → dict con distancia, duración y geometría

    Convención de coordenadas:
        - Todos los argumentos públicos usan (latitud, longitud), igual que
          el resto del proyecto.
        - La conversión a (longitud, latitud) para OSRM se hace internamente
          en _to_osrm_coord().

    Args:
        base_url: URL base del servidor OSRM sin trailing slash.
                  Default: "http://localhost:5000"
        timeout:  Timeout en segundos para cada request HTTP.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:5000",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # Timeout reducido para bearings: son opcionales y no justifican esperar
        # el timeout completo si OSRM está caído. El circuit breaker corta a los
        # 3 fallos, pero reducir el timeout minimiza el daño de cada fallo.
        self._bearing_timeout = min(timeout, 8.0)
        self._session = requests.Session()
        # OSRM_FALLBACK=haversine → usa haversine cuando OSRM no está disponible
        self._fallback = os.environ.get("OSRM_FALLBACK", "").lower() == "haversine"

    # ── API pública ───────────────────────────────────────────

    def get_road_bearings(
        self,
        locations: list[tuple[float, float]],
        depot_indices: set[int] | None = None,
        bearing_range: int = 45,
        _circuit_breaker_threshold: int = 3,
    ) -> list[tuple[int, int] | None]:
        """
        Determina el bearing de la calle en cada ubicación usando OSRM Route API.

        Para cada location, calcula una ruta corta hacia un punto 30m al norte
        y extrae el bearing_after del primer maneuver — esto indica la dirección
        de la calle en ese punto.

        Circuit breaker: si se acumulan _circuit_breaker_threshold fallos
        consecutivos (timeout / connection error), se aborta el loop y se
        retornan None para el resto de las ubicaciones. Esto evita quemar
        N × timeout_s segundos de Lambda cuando OSRM no está disponible.

        Args:
            locations: Lista de (latitud, longitud).
            depot_indices: Índices que son depósitos (sin restricción de bearing).
            bearing_range: Rango en grados a cada lado del bearing (default 45°).
            _circuit_breaker_threshold: Fallos consecutivos antes de abortar.

        Returns:
            Lista de (bearing, range) o None (para depósitos / errores).
        """
        if depot_indices is None:
            depot_indices = set()

        bearings: list[tuple[int, int] | None] = []
        consecutive_failures = 0

        for idx, (lat, lon) in enumerate(locations):
            if idx in depot_indices:
                bearings.append(None)
                continue

            # Circuit breaker: OSRM no está disponible, rellenar con None
            if consecutive_failures >= _circuit_breaker_threshold:
                bearings.append(None)
                continue

            # Punto ~30m al norte para generar una ruta corta
            offset_lat = lat + 0.00027  # ~30m en latitud
            coords = (
                f"{self._to_osrm_coord(lat, lon)};"
                f"{self._to_osrm_coord(offset_lat, lon)}"
            )
            url = f"{self._base_url}/route/v1/driving/{coords}"

            try:
                data = self._get(
                    url,
                    params={"steps": "true", "overview": "false"},
                    timeout=self._bearing_timeout,
                )
                steps = data["routes"][0]["legs"][0]["steps"]
                bearing_after = int(steps[0]["maneuver"]["bearing_after"])
                bearings.append((bearing_after, bearing_range))
                consecutive_failures = 0  # reset al tener éxito
            except Exception as exc:
                consecutive_failures += 1
                logger.debug(
                    "No se pudo obtener bearing para location[%d] (%.6f, %.6f): %s",
                    idx, lat, lon, exc,
                )
                if consecutive_failures == _circuit_breaker_threshold:
                    logger.warning(
                        "OSRM: %d fallos consecutivos en get_road_bearings — "
                        "abortando loop, el resto se optimizará sin bearings",
                        consecutive_failures,
                    )
                bearings.append(None)

        n_resolved = sum(1 for b in bearings if b is not None)
        logger.info(
            "Bearings: %d/%d resueltos (%d depósitos excluidos)",
            n_resolved, len(locations), len(depot_indices),
        )
        return bearings

    def get_distance_matrix(
        self,
        locations: list[tuple[float, float]],
        bearings: list[tuple[int, int] | None] | None = None,
    ) -> dict[str, Any]:
        """
        Calcula la matriz de duración y distancia entre N ubicaciones.

        Llama a /table/v1/driving/{coords}?annotations=duration,distance

        Args:
            locations: Lista de (latitud, longitud). Acepta hasta
                       _MAX_TABLE_SIZE (500) ubicaciones por llamada.
                       Para matrices más grandes, particionar la lista
                       y combinar los resultados en el caller.

        Returns:
            {
                "durations": [[float, ...], ...],  # segundos, N×N
                "distances": [[float, ...], ...],  # metros, N×N
                "locations": [(lat, lon), ...],    # misma lista de entrada
                "n": int,                          # número de ubicaciones
            }

        Raises:
            OSRMError:                  Respuesta con code != "Ok".
            requests.exceptions.Timeout: Servidor no responde en timeout seg.
            requests.exceptions.ConnectionError: Servidor no alcanzable.
            ValueError:                 Lista vacía o demasiado grande.
        """
        if not locations:
            raise ValueError("locations no puede estar vacía")
        if len(locations) > _MAX_TABLE_SIZE:
            raise ValueError(
                f"get_distance_matrix acepta hasta {_MAX_TABLE_SIZE} ubicaciones "
                f"por llamada (recibidas: {len(locations)}). "
                f"Particioná la lista y combiná las sub-matrices en el caller."
            )

        coords_str = self._build_coords(locations)
        url = f"{self._base_url}/table/v1/driving/{coords_str}"

        params: dict[str, str] = {"annotations": "duration,distance"}
        if bearings is not None:
            params["bearings"] = self._build_bearings(bearings)

        logger.debug("Table API: %d locations → GET %s", len(locations), url)

        try:
            data = self._get(url, params=params)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if self._fallback:
                logger.warning(
                    "OSRM no disponible (%s) — usando fallback haversine", exc.__class__.__name__
                )
                return _haversine_matrix(locations)
            raise

        return {
            "durations": data["durations"],
            "distances": data["distances"],
            "locations": locations,
            "n": len(locations),
        }

    def get_route(
        self,
        waypoints: list[tuple[float, float]],
        bearings: list[tuple[int, int] | None] | None = None,
    ) -> dict[str, Any]:
        """
        Calcula la ruta óptima pasando por todos los waypoints en orden.

        Llama a /route/v1/driving/{coords}?overview=full&geometries=geojson&steps=true

        Args:
            waypoints: Lista ordenada de (latitud, longitud).
                       Mínimo 2 puntos.

        Returns:
            {
                "distance_m":  float,        # distancia total en metros
                "duration_s":  float,        # duración total en segundos
                "geometry":    dict,         # GeoJSON LineString (lon, lat)
                "steps":       list[dict],   # pasos de navegación por leg
                "waypoints":   list[tuple],  # misma lista de entrada
            }

        Raises:
            OSRMError:       Respuesta con code != "Ok" o sin rutas.
            ValueError:      Menos de 2 waypoints.
        """
        if len(waypoints) < 2:
            raise ValueError("get_route requiere al menos 2 waypoints")

        coords_str = self._build_coords(waypoints)
        url = f"{self._base_url}/route/v1/driving/{coords_str}"

        logger.debug("Route API: %d waypoints → GET %s", len(waypoints), url)

        params: dict[str, str] = {
            "overview": "full",
            "geometries": "geojson",
            "steps": "true",
        }
        if bearings is not None:
            params["bearings"] = self._build_bearings(bearings)

        data = self._get(url, params=params)

        routes = data.get("routes")
        if not routes:
            raise OSRMError("OSRM no devolvió ninguna ruta")

        route = routes[0]
        steps = [
            step
            for leg in route.get("legs", [])
            for step in leg.get("steps", [])
        ]

        return {
            "distance_m": route["distance"],
            "duration_s": route["duration"],
            "geometry":   route["geometry"],
            "steps":      steps,
            "waypoints":  waypoints,
        }

    # ── Internos ─────────────────────────────────────────────

    @staticmethod
    def _to_osrm_coord(lat: float, lon: float) -> str:
        """
        Convierte (latitud, longitud) del proyecto a "longitud,latitud" de OSRM.
        OSRM usa (lon, lat) en todos sus endpoints.
        """
        return f"{lon},{lat}"

    def _build_coords(self, locations: list[tuple[float, float]]) -> str:
        """Une N ubicaciones en el formato 'lon1,lat1;lon2,lat2;...' de OSRM."""
        return ";".join(self._to_osrm_coord(lat, lon) for lat, lon in locations)

    @staticmethod
    def _build_bearings(bearings: list[tuple[int, int] | None]) -> str:
        """
        Construye el string de bearings para OSRM.
        Formato: "bearing,range;bearing,range;;bearing,range"
        Un elemento vacío ('') significa sin restricción para ese punto.
        """
        parts: list[str] = []
        for b in bearings:
            if b is None:
                parts.append("")
            else:
                parts.append(f"{b[0]},{b[1]}")
        return ";".join(parts)

    def _get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict:
        """
        Ejecuta un GET HTTP y valida que OSRM devuelva code='Ok'.

        Args:
            timeout: Timeout en segundos. Si es None usa self._timeout.

        Raises:
            OSRMError:                       Si code != 'Ok'.
            requests.exceptions.Timeout:     Si el servidor no responde.
            requests.exceptions.ConnectionError: Si no se puede conectar.
            requests.exceptions.HTTPError:   Si el servidor devuelve 4xx/5xx.
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            resp = self._session.get(url, params=params, timeout=effective_timeout)
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            logger.error("OSRM timeout (%.0fs) en %s", effective_timeout, url)
            raise
        except requests.exceptions.ConnectionError:
            logger.error("OSRM no alcanzable en %s", self._base_url)
            raise

        data: dict = resp.json()
        code = data.get("code")
        if code != "Ok":
            msg = data.get("message", "sin mensaje")
            logger.error("OSRM error: code=%s message=%s url=%s", code, msg, url)
            raise OSRMError(f"OSRM code={code!r}: {msg}")

        return data


# ─────────────────────────────────────────────────────────
# Demo / smoke test
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s — %(message)s",
    )

    # 5 puntos representativos de Montevideo
    # Convención del proyecto: (latitud, longitud)
    POINTS: list[tuple[str, float, float]] = [
        ("Centro",          -34.9059, -56.1913),
        ("Pocitos",         -34.9008, -56.1526),
        ("Cerro",           -34.8924, -56.2476),
        ("Felipe Cardoso",  -34.8347, -56.0967),  # depósito noreste
        ("Ruta 102",        -34.8128, -56.2645),  # estación transferencia oeste
    ]

    labels    = [p[0]        for p in POINTS]
    locations = [(p[1], p[2]) for p in POINTS]

    client = OSRMClient()

    # ── Matriz de distancias ───────────────────────────────
    print("=" * 60)
    print("Matriz de duraciones (minutos)")
    print("=" * 60)

    try:
        result = client.get_distance_matrix(locations)
    except (OSRMError, requests.exceptions.RequestException) as exc:
        print(f"ERROR: {exc}")
        print("¿Está corriendo el servidor OSRM?")
        print("  cd osrm && docker compose up -d osrm-server")
        sys.exit(1)

    durations = result["durations"]
    distances = result["distances"]
    n = result["n"]

    # Encabezado
    col_w = 16
    print(f"{'':20}", end="")
    for label in labels:
        print(f"{label[:col_w]:>{col_w}}", end="")
    print()
    print("-" * (20 + col_w * n))

    for i, src in enumerate(labels):
        print(f"{src:<20}", end="")
        for j in range(n):
            mins = durations[i][j] / 60
            print(f"{mins:>{col_w}.1f}", end="")
        print()

    print()

    # ── Distancias (km) ────────────────────────────────────
    print("=" * 60)
    print("Matriz de distancias (km)")
    print("=" * 60)

    print(f"{'':20}", end="")
    for label in labels:
        print(f"{label[:col_w]:>{col_w}}", end="")
    print()
    print("-" * (20 + col_w * n))

    for i, src in enumerate(labels):
        print(f"{src:<20}", end="")
        for j in range(n):
            km = distances[i][j] / 1000
            print(f"{km:>{col_w}.1f}", end="")
        print()

    print()

    # ── Ruta Centro → Cerro → Felipe Cardoso ──────────────
    print("=" * 60)
    print("Ruta: Centro → Cerro → Felipe Cardoso")
    print("=" * 60)

    route_points = [locations[0], locations[2], locations[3]]
    route = client.get_route(route_points)

    print(f"  Distancia total : {route['distance_m'] / 1000:.1f} km")
    print(f"  Duración total  : {route['duration_s'] / 60:.1f} min")
    print(f"  Pasos de ruta   : {len(route['steps'])}")
    print(f"  Tipo geometría  : {route['geometry']['type']}")
    coords_count = len(route["geometry"]["coordinates"])
    print(f"  Puntos GeoJSON  : {coords_count}")
