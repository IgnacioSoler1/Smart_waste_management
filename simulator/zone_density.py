"""
zone_density.py — SmartWaste MVD

Asigna un factor de densidad poblacional (zone_factor) a cada contenedor
según su municipio, usando bounding boxes aproximados calibrados con los
centroides reales de los 133 circuitos de recolección de Montevideo.

El zone_factor representa la velocidad relativa de llenado:
  - 2.5 → centro histórico y barrios densos (municipio B)
  - 2.0 → costa este, barrios residenciales de alta densidad (CH)
  - 1.5 → zona intermedia (C)
  - 1.0 → periferia media (E, F)
  - 0.7 → periferia exterior (A, G, D)

Uso:
  from simulator.zone_density import get_zone_factor
  factor = get_zone_factor(-34.905, -56.185)  # → 2.5 (municipio B)
"""

# ─────────────────────────────────────────────────────────
# Definición de zonas municipales
#
# Cada entrada: (código, lat_min, lat_max, lon_min, lon_max, factor)
# Calibrado con los rangos de centroides de circuitos por municipio.
# Se chequean en orden de prioridad (mayor densidad primero) para
# resolver solapamientos en los límites.
#
# Fuente de rangos (data/processed/circuits_enriched.json):
#   B:  lat [-34.9176, -34.8912]  lon [-56.2038, -56.1641]
#   CH: lat [-34.9222, -34.8850]  lon [-56.1623, -56.1308]
#   C:  lat [-34.8868, -34.8554]  lon [-56.2093, -56.1604]
#   E:  lat [-34.8929, -34.8723]  lon [-56.1448, -56.0502]
#   F:  lat [-34.8673, -34.8024]  lon [-56.1463, -56.0878]
#   A:  lat [-34.8905, -34.8377]  lon [-56.2746, -56.2172]
#   G:  lat [-34.8471, -34.7920]  lon [-56.2450, -56.2005]
#   D:  lat [-34.8771, -34.7769]  lon [-56.1766, -56.1363]
# ─────────────────────────────────────────────────────────

# (code, lat_min, lat_max, lon_min, lon_max, zone_factor)
_ZONE_TABLE: list[tuple[str, float, float, float, float, float]] = [
    # ── Alta densidad ─────────────────────────────────────
    # Municipio B: Centro, Cordón, Ciudad Vieja
    #   Barrio más denso; mercados, oficinas, vida nocturna
    ("B",  -34.930, -34.880, -56.215, -56.150, 2.5),

    # Municipio CH: Pocitos, Buceo, Punta Carretas
    #   Costa este; alta densidad residencial y comercial
    ("CH", -34.935, -34.875, -56.175, -56.115, 2.0),

    # ── Densidad media ────────────────────────────────────
    # Municipio C: Unión, Malvín
    #   Zona intermedia norte del centro
    ("C",  -34.900, -34.840, -56.225, -56.148, 1.5),

    # Municipio E: Este (hacia Carrasco/Punta Gorda)
    #   Periferia este con algunos nodos comerciales
    ("E",  -34.910, -34.860, -56.160, -56.035, 1.0),

    # Municipio F: Norte (Flor de Maroñas, Maroñas)
    #   Zona norte de densidad media-baja
    ("F",  -34.880, -34.790, -56.160, -56.075, 1.0),

    # ── Periferia ─────────────────────────────────────────
    # Municipio A: Oeste (Cerro, La Paloma, Casabó)
    #   Zona obrera oeste, menor densidad relativa
    ("A",  -34.910, -34.825, -56.290, -56.205, 0.7),

    # Municipio G: Noroeste (Colón, Lezica)
    #   Suburbano, menor densidad comercial
    ("G",  -34.860, -34.780, -56.260, -56.185, 0.7),

    # Municipio D: Norte-centro (Sayago, Jacinto Vera)
    #   Periferia norte semi-urbanizada
    ("D",  -34.890, -34.765, -56.195, -56.120, 0.7),
]

# Factor por defecto si las coordenadas no caen en ningún bounding box
_DEFAULT_FACTOR: float = 1.0


def get_zone_factor(latitude: float, longitude: float) -> float:
    """
    Retorna el factor de densidad para un par de coordenadas WGS84.

    Usa una tabla de bounding boxes municipales calibrada con los datos
    reales de los 133 circuitos de recolección de Montevideo. La precisión
    es suficiente para la simulación (~1-2 km de error en los límites).

    Args:
        latitude:  latitud WGS84 (negativa para el hemisferio sur)
        longitude: longitud WGS84 (negativa para el oeste)

    Returns:
        Factor entre 0.5 (suburbano) y 3.0 (centro). En la práctica
        los valores van de 0.7 a 2.5 según los municipios definidos.
        Retorna 1.0 si las coordenadas están fuera de todos los polígonos.

    Examples:
        >>> get_zone_factor(-34.905, -56.185)   # Centro
        2.5
        >>> get_zone_factor(-34.900, -56.140)   # Pocitos
        2.0
        >>> get_zone_factor(-34.870, -56.245)   # Cerro
        0.7
    """
    for _code, lat_min, lat_max, lon_min, lon_max, factor in _ZONE_TABLE:
        if lat_min <= latitude <= lat_max and lon_min <= longitude <= lon_max:
            return factor
    return _DEFAULT_FACTOR


def get_municipality(latitude: float, longitude: float) -> str:
    """
    Retorna el código de municipio aproximado para las coordenadas dadas.

    Útil para logs y debugging. Retorna "?" si no hay coincidencia.
    """
    for code, lat_min, lat_max, lon_min, lon_max, _factor in _ZONE_TABLE:
        if lat_min <= latitude <= lat_max and lon_min <= longitude <= lon_max:
            return code
    return "?"
