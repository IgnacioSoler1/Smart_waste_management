#!/usr/bin/env python3
"""
consolidate_data.py — SmartWaste MVD

Consolida los datos procesados de contenedores y circuitos en archivos JSON
enriquecidos listos para usar por el route-optimizer y el simulador.

Genera:
  data/processed/circuits_enriched.json   — un objeto por circuito, con
      conteo de contenedores activos, centroide, turno predominante,
      días de recolección, zona (este/oeste) y depot asignado.

  data/processed/containers_enriched.json — lista de contenedores activos
      con zona y depot heredados del circuito.

Uso:
  python data/scripts/consolidate_data.py
  python data/scripts/consolidate_data.py --processed-dir data/processed/

Dependencias: pandas (stdlib: json, pathlib, argparse, collections)
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# ─────────────────────────────────────────────────────────
# Constantes del dominio
# ─────────────────────────────────────────────────────────

# Límite longitudinal este/oeste de Montevideo (aprox. -56.17)
EAST_WEST_BOUNDARY_LON: float = -56.17

DEPOTS: dict[str, dict] = {
    "west": {
        "id": "depot_estacion_transferencia",
        "name": "Estación de Transferencia (Ruta 102)",
        "latitude": -34.8128,
        "longitude": -56.2645,
        "type": "transfer_station",
    },
    "east": {
        "id": "depot_felipe_cardoso",
        "name": "Sitio Disposición Final Felipe Cardoso",
        "latitude": -34.8347,
        "longitude": -56.0967,
        "type": "disposal_site",
    },
}

# Bounding box de Montevideo en WGS84
MVD_BOUNDS: dict[str, float] = {
    "lat_min": -34.95,
    "lat_max": -34.70,
    "lon_min": -56.40,
    "lon_max": -55.95,
}

# Mapeo de palabras clave del schedule a turno normalizado
SHIFT_KEYWORDS: list[tuple[str, str]] = [
    ("matutino", "morning"),
    ("vespertino", "afternoon"),
    ("nocturno", "night"),
]

# Días de la semana reconocidos en el schedule
WEEKDAYS_ES: list[str] = [
    "LUNES", "MARTES", "MIERCOLES", "MIÉRCOLES",
    "JUEVES", "VIERNES", "SABADO", "SÁBADO", "DOMINGO", "DOMINGOS",
]

# Normalización de variantes ortográficas a nombre canónico
WEEKDAY_NORMALIZE: dict[str, str] = {
    "MIÉRCOLES": "MIERCOLES",
    "SÁBADO": "SABADO",
    "SABADOS": "SABADO",
    "DOMINGOS": "DOMINGO",
    "LUNES,": "LUNES",
}


# ─────────────────────────────────────────────────────────
# Parsing del schedule
# ─────────────────────────────────────────────────────────

def parse_shift(schedule: str) -> str:
    """
    Extrae el turno (morning / afternoon / night) desde el texto del schedule.

    Formatos comunes en los datos de la Intendencia:
      "LUNES, MIERCOLES Y VIERNES CON FERIADOS LABORABLES: Matutino (06 a 14 hrs.)"
      "MARTES JUEVES Y SABADOS CON FERIADOS LABORABLES: Vespertino (14 a 22 hrs.)"
    """
    if not isinstance(schedule, str) or not schedule.strip():
        return "UNKNOWN"
    lower = schedule.lower()
    for keyword, shift in SHIFT_KEYWORDS:
        if keyword in lower:
            return shift
    return "UNKNOWN"


def parse_collection_days(schedule: str) -> list[str]:
    """
    Extrae la lista de días de recolección desde el texto del schedule.

    Retorna una lista canónica, p.ej. ["LUNES", "MIERCOLES", "VIERNES"].
    """
    if not isinstance(schedule, str) or not schedule.strip():
        return []

    upper = schedule.upper()
    found: list[str] = []
    for day in WEEKDAYS_ES:
        # Buscar la palabra completa (con posible coma/espacio después)
        if re.search(rf"\b{re.escape(day)}\b", upper):
            canonical = WEEKDAY_NORMALIZE.get(day, day)
            if canonical not in found:
                found.append(canonical)
    return found


# ─────────────────────────────────────────────────────────
# Asignación de zona y depot
# ─────────────────────────────────────────────────────────

def assign_zone(centroid_lon: float) -> str:
    """Asigna 'west' o 'east' según la longitud del centroide del circuito."""
    return "west" if centroid_lon < EAST_WEST_BOUNDARY_LON else "east"


def assign_depot(zone: str) -> dict:
    """Retorna el depot correspondiente a la zona."""
    return DEPOTS[zone]


# ─────────────────────────────────────────────────────────
# Consolidación de circuitos
# ─────────────────────────────────────────────────────────

def build_circuits_enriched(
    containers_df: pd.DataFrame,
    centroids_df: pd.DataFrame | None,
) -> dict[str, dict]:
    """
    Construye el diccionario de circuitos enriquecidos.

    Para cada circuit_id calcula:
      - container_count: cantidad de contenedores activos
      - centroid_lat / centroid_lon: promedio de coords de los contenedores activos
          (o del CSV de centroides si está disponible y el circuito no tiene contenedores)
      - dominant_shift: turno más frecuente
      - collection_days: unión de días de recolección de los contenedores
      - zone: 'east' | 'west'
      - depot: dict con id, name, latitude, longitude, type
      - municipality: del CSV de centroides si disponible
    """
    active = containers_df[containers_df["status"] == "active"].copy()

    # Pre-procesar shift y collection_days desde el campo schedule cuando
    # el shift almacenado es UNKNOWN (los datos reales de la Intendencia usan
    # texto completo, no las abreviaturas M/V/N que esperaba el parser original)
    active = active.assign(
        _parsed_shift=active.apply(
            lambda r: (
                parse_shift(str(r.get("schedule", "")))
                if str(r.get("shift", "UNKNOWN")).upper() == "UNKNOWN"
                else str(r["shift"])
            ),
            axis=1,
        ),
        _parsed_days=active["schedule"].apply(
            lambda s: parse_collection_days(str(s))
        ),
    )

    # Índice de circuitos del CSV de centroides (para municipio y fallback de centroide)
    centroids_index: dict[str, dict] = {}
    if centroids_df is not None:
        for _, row in centroids_df.iterrows():
            cid = str(row["circuit_id"]).strip()
            centroids_index[cid] = {
                "municipality": str(row.get("municipality", "")).strip(),
                "centroid_lat": float(row["centroid_lat"]),
                "centroid_lon": float(row["centroid_lon"]),
            }

    circuits: dict[str, dict] = {}

    # Agrupar por circuit_id
    grouped = active.groupby("circuit_id")

    for circuit_id, group in grouped:
        cid = str(circuit_id).strip()

        # Centroide desde los contenedores activos
        centroid_lat = round(float(group["latitude"].mean()), 6)
        centroid_lon = round(float(group["longitude"].mean()), 6)

        # Turno predominante
        shift_counts = Counter(group["_parsed_shift"].tolist())
        dominant_shift = shift_counts.most_common(1)[0][0]

        # Unión de días de recolección
        all_days: list[str] = []
        for days in group["_parsed_days"]:
            for d in days:
                if d not in all_days:
                    all_days.append(d)

        # Municipio desde centroids si disponible
        municipality = centroids_index.get(cid, {}).get("municipality", "")

        zone = assign_zone(centroid_lon)
        depot = assign_depot(zone)

        circuits[cid] = {
            "circuit_id": cid,
            "municipality": municipality,
            "container_count": int(len(group)),
            "centroid_lat": centroid_lat,
            "centroid_lon": centroid_lon,
            "dominant_shift": dominant_shift,
            "collection_days": all_days,
            "zone": zone,
            "depot": depot,
        }

    # Agregar circuitos que están en centroids pero no tienen contenedores activos
    for cid, centroid_data in centroids_index.items():
        if cid not in circuits:
            centroid_lon = centroid_data["centroid_lon"]
            zone = assign_zone(centroid_lon)
            circuits[cid] = {
                "circuit_id": cid,
                "municipality": centroid_data["municipality"],
                "container_count": 0,
                "centroid_lat": centroid_data["centroid_lat"],
                "centroid_lon": centroid_lon,
                "dominant_shift": "UNKNOWN",
                "collection_days": [],
                "zone": zone,
                "depot": assign_depot(zone),
            }

    return circuits


# ─────────────────────────────────────────────────────────
# Consolidación de contenedores
# ─────────────────────────────────────────────────────────

def build_containers_enriched(
    containers_df: pd.DataFrame,
    circuits: dict[str, dict],
) -> list[dict]:
    """
    Construye la lista de contenedores enriquecidos.

    Cada contenedor hereda zona y depot de su circuito.
    Solo se incluyen contenedores activos con coordenadas válidas.
    """
    active = containers_df[
        (containers_df["status"] == "active")
        & (containers_df["coord_valid"].astype(str).str.lower() == "true")
    ].copy()

    containers: list[dict] = []

    # Contador de secuencia por circuito (orden del CSV original)
    circuit_seq: dict[str, int] = {}

    for _, row in active.iterrows():
        cid = str(row["circuit_id"]).strip()
        circuit = circuits.get(cid, {})

        zone = circuit.get("zone", "UNKNOWN")
        depot = circuit.get("depot", {})
        dominant_shift = circuit.get("dominant_shift", "UNKNOWN")

        # Shift a nivel de contenedor (re-parsear si es UNKNOWN)
        container_shift = str(row.get("shift", "UNKNOWN"))
        if container_shift.upper() == "UNKNOWN":
            container_shift = parse_shift(str(row.get("schedule", "")))
        # Fallback al turno del circuito
        if container_shift == "UNKNOWN" and dominant_shift != "UNKNOWN":
            container_shift = dominant_shift

        collection_days = parse_collection_days(str(row.get("schedule", "")))

        # csv_sequence: posición del contenedor dentro de su circuito
        # según el orden original del CSV de la Intendencia
        seq = circuit_seq.get(cid, 0)
        circuit_seq[cid] = seq + 1

        containers.append({
            "container_id": str(row["container_id"]),
            "circuit_id": cid,
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
            "shift": container_shift,
            "collection_days": collection_days,
            "zone": zone,
            "depot_id": depot.get("id", ""),
            "status": "active",
            "csv_sequence": seq,
        })

    return containers


# ─────────────────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────────────────

def load_containers(processed_dir: Path) -> pd.DataFrame:
    path = processed_dir / "containers_wgs84.csv"
    if not path.exists():
        print(f"❌ No se encontró: {path}", file=sys.stderr)
        sys.exit(1)
    df = pd.read_csv(path, dtype=str)
    # Convertir columnas numéricas
    numeric_cols = [c for c in ("latitude", "longitude", "x_utm", "y_utm") if c in df.columns]
    df = df.assign(**{col: pd.to_numeric(df[col], errors="coerce") for col in numeric_cols})
    print(f"   Contenedores leídos: {len(df):,}  ({path})")
    return df


def load_centroids(processed_dir: Path) -> pd.DataFrame | None:
    path = processed_dir / "circuits_centroids.csv"
    if not path.exists():
        print("   circuits_centroids.csv no encontrado — se usarán solo los datos de contenedores.")
        return None
    df = pd.read_csv(path, dtype=str)
    numeric_cols = [c for c in ("centroid_lat", "centroid_lon") if c in df.columns]
    df = df.assign(**{col: pd.to_numeric(df[col], errors="coerce") for col in numeric_cols})
    print(f"   Centroides de circuitos leídos: {len(df):,}  ({path})")
    return df


def save_json(data: dict | list, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    size_kb = path.stat().st_size / 1024
    count = len(data) if isinstance(data, (dict, list)) else "?"
    print(f"   ✅ {path.name}: {count} entradas  ({size_kb:.1f} KB)")


# ─────────────────────────────────────────────────────────
# Estadísticas de resumen
# ─────────────────────────────────────────────────────────

def print_summary(circuits: dict[str, dict], containers: list[dict]) -> None:
    total_circuits = len(circuits)
    circuits_with_containers = sum(1 for c in circuits.values() if c["container_count"] > 0)
    east_circuits = sum(1 for c in circuits.values() if c["zone"] == "east")
    west_circuits = sum(1 for c in circuits.values() if c["zone"] == "west")

    shift_counts: Counter = Counter(c["dominant_shift"] for c in circuits.values())

    total_containers = len(containers)
    east_containers = sum(1 for c in containers if c["zone"] == "east")
    west_containers = sum(1 for c in containers if c["zone"] == "west")

    print()
    print("─" * 55)
    print("  Resumen de consolidación")
    print("─" * 55)
    print(f"  Circuitos totales:          {total_circuits:>5}")
    print(f"    Con contenedores activos: {circuits_with_containers:>5}")
    print(f"    Zona este:                {east_circuits:>5}")
    print(f"    Zona oeste:               {west_circuits:>5}")
    print(f"  Turno predominante:")
    for shift, count in shift_counts.most_common():
        print(f"    {shift:<12}             {count:>5}")
    print()
    print(f"  Contenedores activos:       {total_containers:>5}")
    print(f"    Zona este:                {east_containers:>5}")
    print(f"    Zona oeste:               {west_containers:>5}")
    print("─" * 55)


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consolida datos de contenedores y circuitos en JSON enriquecidos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplo:
  python data/scripts/consolidate_data.py
  python data/scripts/consolidate_data.py --processed-dir data/processed/
        """,
    )
    parser.add_argument(
        "--processed-dir",
        default="data/processed",
        help="Directorio con los CSV procesados (default: data/processed/)",
    )
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)

    print("=" * 55)
    print("  SmartWaste MVD — Consolidación de datos")
    print("=" * 55)
    print()

    # Cargar datos
    containers_df = load_containers(processed_dir)
    centroids_df = load_centroids(processed_dir)

    print()
    print("  Construyendo circuitos enriquecidos...")
    circuits = build_circuits_enriched(containers_df, centroids_df)

    print("  Construyendo contenedores enriquecidos...")
    containers = build_containers_enriched(containers_df, circuits)

    print()
    print("  Guardando archivos JSON...")
    save_json(circuits, processed_dir / "circuits_enriched.json")
    save_json(containers, processed_dir / "containers_enriched.json")

    print_summary(circuits, containers)

    print()
    print("  ✅ Consolidación completada.")
    print(f"     Archivos en: {processed_dir}/")
    print("=" * 55)


if __name__ == "__main__":
    main()
