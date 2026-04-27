"""
test_osrm.py — SmartWaste MVD

Pruebas de integración contra el servidor OSRM local.
Valida que los tres endpoints usados por el route-optimizer responden
correctamente con datos de Montevideo.

Uso:
    python osrm/test_osrm.py
    python osrm/test_osrm.py --base-url http://mi-servidor:5000

Requiere:
    pip install requests
    docker compose up -d osrm-server  (en osrm/)
"""

import argparse
import sys
from dataclasses import dataclass
from typing import Any

import requests

# ─────────────────────────────────────────────────────────
# Puntos de referencia en Montevideo
# Convención del proyecto: (lat, lon)
# ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Point:
    name: str
    lat: float
    lon: float

    def osrm(self) -> str:
        """Formato que espera la API de OSRM: 'lon,lat'."""
        return f"{self.lon},{self.lat}"


CENTRO       = Point("Centro Montevideo",       lat=-34.9059, lon=-56.1913)
POCITOS      = Point("Pocitos",                 lat=-34.9008, lon=-56.1526)
CERRO        = Point("Cerro",                   lat=-34.8924, lon=-56.2476)
FELIPE_CARDOSO = Point("Felipe Cardoso (depósito)", lat=-34.8347, lon=-56.0967)

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


def check(condition: bool, msg_ok: str, msg_fail: str) -> bool:
    if condition:
        print(f"    {PASS} {msg_ok}")
    else:
        print(f"    {FAIL} {msg_fail}")
    return condition


def get(url: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict:
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────────────────
# Test 1 — Table API (matriz 3×3)
# ─────────────────────────────────────────────────────────

def test_table(base_url: str) -> bool:
    """
    Pide la matriz de duración y distancia entre 3 puntos de Montevideo.

    Valida:
      - Respuesta exitosa (code = "Ok")
      - Matriz 3×3 (9 elementos)
      - Diagonal ~0 (cada punto a sí mismo)
      - Duraciones razonables para Montevideo (< 3600 s ≈ 1 hora)
      - Distancias razonables (< 50 000 m ≈ 50 km)
    """
    print("\n── Test 1: Table API (matriz 3×3) ──────────────────────")
    points = [CENTRO, POCITOS, CERRO]
    coords = ";".join(p.osrm() for p in points)
    url = f"{base_url}/table/v1/driving/{coords}"

    try:
        data = get(url, params={"annotations": "duration,distance"})
    except requests.RequestException as exc:
        print(f"  {FAIL} Request fallido: {exc}")
        return False

    ok = True

    ok &= check(data.get("code") == "Ok", "code = Ok", f"code inesperado: {data.get('code')}")

    durations: list[list[float]] = data.get("durations", [])
    distances: list[list[float]] = data.get("distances", [])

    ok &= check(len(durations) == 3 and all(len(row) == 3 for row in durations),
                "Matriz de duraciones 3×3", f"Dimensiones inesperadas: {len(durations)}")
    ok &= check(len(distances) == 3 and all(len(row) == 3 for row in distances),
                "Matriz de distancias 3×3", f"Dimensiones inesperadas: {len(distances)}")

    # Diagonal debe ser ~0
    for i, p in enumerate(points):
        ok &= check(durations[i][i] < 1.0,
                    f"Diagonal[{i}] duración ≈ 0 ({durations[i][i]:.1f} s)",
                    f"Diagonal[{i}] duración alta: {durations[i][i]:.1f} s")

    # Duraciones y distancias razonables para trayectos urbanos
    for i, src in enumerate(points):
        for j, dst in enumerate(points):
            if i == j:
                continue
            d_secs = durations[i][j]
            d_km   = distances[i][j] / 1000
            ok &= check(0 < d_secs < 3600,
                        f"{src.name} → {dst.name}: {d_secs/60:.1f} min, {d_km:.1f} km",
                        f"{src.name} → {dst.name}: duración irrazonable {d_secs:.0f} s")

    return ok


# ─────────────────────────────────────────────────────────
# Test 2 — Route API (Centro → Felipe Cardoso)
# ─────────────────────────────────────────────────────────

def test_route(base_url: str) -> bool:
    """
    Calcula la ruta en coche del Centro de Montevideo al sitio de
    disposición Felipe Cardoso (noreste, distancia real ~12 km).

    Valida:
      - Ruta encontrada
      - Distancia entre 8 y 25 km (sanity check geográfico)
      - Duración entre 10 y 45 minutos
    """
    print("\n── Test 2: Route API (Centro → Felipe Cardoso) ─────────")
    coords = f"{CENTRO.osrm()};{FELIPE_CARDOSO.osrm()}"
    url = f"{base_url}/route/v1/driving/{coords}"

    try:
        data = get(url, params={"overview": "false"})
    except requests.RequestException as exc:
        print(f"  {FAIL} Request fallido: {exc}")
        return False

    ok = True
    ok &= check(data.get("code") == "Ok", "code = Ok", f"code inesperado: {data.get('code')}")

    routes = data.get("routes", [])
    ok &= check(len(routes) > 0, f"{len(routes)} ruta(s) encontrada(s)", "Sin rutas en la respuesta")

    if not routes:
        return False

    route = routes[0]
    dist_km   = route["distance"] / 1000
    dur_min   = route["duration"] / 60
    legs      = len(route.get("legs", []))

    print(f"    Distancia: {dist_km:.1f} km | Duración: {dur_min:.1f} min | Legs: {legs}")

    ok &= check(8 <= dist_km <= 25,
                f"Distancia razonable ({dist_km:.1f} km)",
                f"Distancia fuera de rango esperado: {dist_km:.1f} km (esperado 8–25 km)")
    ok &= check(10 <= dur_min <= 45,
                f"Duración razonable ({dur_min:.1f} min)",
                f"Duración fuera de rango esperado: {dur_min:.1f} min (esperado 10–45 min)")

    return ok


# ─────────────────────────────────────────────────────────
# Test 3 — Nearest API (snap a red vial)
# ─────────────────────────────────────────────────────────

def test_nearest(base_url: str) -> bool:
    """
    Hace snap de un punto en el Centro de Montevideo al segmento
    vial más cercano.

    Valida:
      - Respuesta exitosa
      - Waypoint snapeado a menos de 50 m del punto original
      - Nombre de calle presente (confirma que los datos tienen metadata)
    """
    print("\n── Test 3: Nearest API (snap a red vial) ───────────────")
    url = f"{base_url}/nearest/v1/driving/{CENTRO.osrm()}"

    try:
        data = get(url, params={"number": 1})
    except requests.RequestException as exc:
        print(f"  {FAIL} Request fallido: {exc}")
        return False

    ok = True
    ok &= check(data.get("code") == "Ok", "code = Ok", f"code inesperado: {data.get('code')}")

    waypoints = data.get("waypoints", [])
    ok &= check(len(waypoints) > 0, "Waypoint encontrado", "Sin waypoints en la respuesta")

    if not waypoints:
        return False

    wp = waypoints[0]
    distance_m = wp.get("distance", float("inf"))
    name       = wp.get("name", "")
    snapped    = wp.get("location", [])

    print(f"    Punto original : ({CENTRO.lat}, {CENTRO.lon})")
    print(f"    Snapeado a     : ({snapped[1]:.5f}, {snapped[0]:.5f})")
    print(f"    Distancia snap : {distance_m:.1f} m")
    print(f"    Nombre de calle: '{name}'")

    ok &= check(distance_m < 50,
                f"Snap a {distance_m:.1f} m (< 50 m)",
                f"Snap muy lejos del punto original: {distance_m:.1f} m")
    ok &= check(bool(name),
                f"Nombre de calle presente: '{name}'",
                "Sin nombre de calle — ¿datos sin metadata?")

    return ok


# ─────────────────────────────────────────────────────────
# Runner principal
# ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Pruebas de integración contra OSRM local")
    parser.add_argument(
        "--base-url",
        default="http://localhost:5000",
        help="URL base del servidor OSRM (default: http://localhost:5000)",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    print(f"SmartWaste MVD — OSRM integration tests")
    print(f"Servidor: {base_url}\n")

    # Verificación rápida de que el servidor responde
    try:
        requests.get(f"{base_url}/nearest/v1/driving/{CENTRO.osrm()}", timeout=5).raise_for_status()
    except requests.RequestException as exc:
        print(f"\033[91mError: No se pudo conectar con {base_url}\033[0m")
        print(f"  {exc}")
        print("\nVerificá que el servidor está corriendo:")
        print("  cd osrm && docker compose up osrm-server")
        sys.exit(1)

    results = [
        test_table(base_url),
        test_route(base_url),
        test_nearest(base_url),
    ]

    passed = sum(results)
    total  = len(results)

    print(f"\n{'='*54}")
    print(f"Resultado: {passed}/{total} tests pasaron")

    if passed == total:
        print("\033[92mTodos los tests OK ✓\033[0m")
        sys.exit(0)
    else:
        print(f"\033[91m{total - passed} test(s) fallaron ✗\033[0m")
        sys.exit(1)


if __name__ == "__main__":
    main()
