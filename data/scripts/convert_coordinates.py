#!/usr/bin/env python3
"""
convert_coordinates.py — SmartWaste MVD

Convierte los CSV de la Intendencia de Montevideo (coordenadas UTM 21S, SIRGAS2000)
a CSV con coordenadas WGS84 (lat/lon) listos para usar en el sistema.

Fuente de datos:
  https://catalogodatos.gub.uy/dataset/contenedores-de-residuos-domiciliarios-ubicacion-circuitos-y-frecuencia-de-recoleccion

Uso:
  python convert_coordinates.py --containers raw/contenedores.csv --output processed/
  python convert_coordinates.py --containers raw/contenedores.csv --circuits raw/circuitos.csv --output processed/

Dependencias:
  pip install pyproj pandas
"""

import argparse
import sys
import os
import json
from pathlib import Path

import pandas as pd
from pyproj import Transformer

# ─────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────

# EPSG:31981 = SIRGAS 2000 / UTM zone 21S (el CRS de la Intendencia)
# EPSG:4326  = WGS84 (lat/lon, lo que usan Google Maps, Leaflet, OSRM, etc.)
SOURCE_CRS = "EPSG:31981"
TARGET_CRS = "EPSG:4326"

# Bounding box aproximado de Montevideo en WGS84 para validación
MVD_BOUNDS = {
    "lat_min": -34.95,
    "lat_max": -34.70,
    "lon_min": -56.40,
    "lon_max": -55.95,
}

# Ubicaciones fijas relevantes al sistema (ya en WGS84)
FIXED_LOCATIONS = {
    "depot_felipe_cardoso": {
        "name": "Sitio Disposición Final Felipe Cardoso",
        "latitude": -34.8347,
        "longitude": -56.0967,
        "type": "disposal_site",
    },
    "depot_estacion_transferencia": {
        "name": "Estación de Transferencia (Ruta 102)",
        "latitude": -34.8128,
        "longitude": -56.2645,
        "type": "transfer_station",
    },
}


# ─────────────────────────────────────────────────────────
# Transformación de coordenadas
# ─────────────────────────────────────────────────────────

def create_transformer() -> Transformer:
    """Crea el transformer UTM 21S → WGS84 (siempre en orden x,y → lon,lat)."""
    return Transformer.from_crs(SOURCE_CRS, TARGET_CRS, always_xy=True)


def utm_to_wgs84(transformer: Transformer, x: float, y: float) -> tuple[float, float]:
    """
    Convierte una coordenada UTM 21S a WGS84.
    
    Args:
        x: Easting (coordenada X en UTM)
        y: Northing (coordenada Y en UTM)
    
    Returns:
        (latitude, longitude) en WGS84
    """
    lon, lat = transformer.transform(x, y)
    return lat, lon


def is_valid_montevideo_coord(lat: float, lon: float) -> bool:
    """Valida que una coordenada caiga dentro del bounding box de Montevideo."""
    return (
        MVD_BOUNDS["lat_min"] <= lat <= MVD_BOUNDS["lat_max"]
        and MVD_BOUNDS["lon_min"] <= lon <= MVD_BOUNDS["lon_max"]
    )


# ─────────────────────────────────────────────────────────
# Procesamiento de contenedores
# ─────────────────────────────────────────────────────────

def process_containers(input_path: str, output_dir: str) -> pd.DataFrame:
    """
    Lee el CSV de contenedores de la Intendencia y genera un CSV limpio con WGS84.
    
    Estructura del CSV de entrada (campos según desc_contenedores.txt):
        gid              - Clave del punto de ubicación del contenedor
        cod_circuito     - Código del circuito de recolección
        turno_horario    - Días y turno planificado de recolección
        motivo           - Motivo de inactividad (vacío si activo)
        x                - Coordenada X (Easting, UTM 21S)
        y                - Coordenada Y (Northing, UTM 21S)
    
    CSV de salida:
        container_id, circuit_id, schedule, status, x_utm, y_utm, latitude, longitude
    """
    print(f"\n📦 Procesando contenedores: {input_path}")
    
    # Leer CSV — la Intendencia usa distintos separadores según el dump
    # Intentamos con coma, punto y coma, y tab
    df = _read_csv_flexible(input_path)
    
    print(f"   Filas leídas: {len(df)}")
    print(f"   Columnas: {list(df.columns)}")
    
    # Normalizar nombres de columnas (lowercase, strip whitespace)
    df.columns = [c.strip().lower() for c in df.columns]
    
    # Verificar columnas requeridas
    required = {"gid", "x", "y"}
    missing = required - set(df.columns)
    if missing:
        print(f"   ⚠️  Columnas faltantes: {missing}")
        print(f"   Columnas disponibles: {list(df.columns)}")
        print(f"   Intentando mapear nombres alternativos...")
        df = _try_remap_columns(df)
    
    # Limpiar datos
    initial_count = len(df)
    
    # Convertir x, y a numérico (por si vienen como string)
    df["x"] = pd.to_numeric(df["x"], errors="coerce")
    df["y"] = pd.to_numeric(df["y"], errors="coerce")
    
    # Eliminar filas sin coordenadas
    df = df.dropna(subset=["x", "y"])
    dropped_na = initial_count - len(df)
    if dropped_na > 0:
        print(f"   ⚠️  Eliminadas {dropped_na} filas sin coordenadas válidas")
    
    # Eliminar filas con coordenadas = 0
    df = df[(df["x"] != 0) & (df["y"] != 0)]
    
    # Transformar coordenadas
    transformer = create_transformer()
    print("   🔄 Convirtiendo UTM 21S → WGS84...")
    
    coords = df.apply(
        lambda row: utm_to_wgs84(transformer, row["x"], row["y"]),
        axis=1,
        result_type="expand",
    )
    df["latitude"] = coords[0]
    df["longitude"] = coords[1]
    
    # Validar que caen en Montevideo
    valid_mask = df.apply(
        lambda row: is_valid_montevideo_coord(row["latitude"], row["longitude"]),
        axis=1,
    )
    invalid_count = (~valid_mask).sum()
    if invalid_count > 0:
        print(f"   ⚠️  {invalid_count} coordenadas fuera del bounding box de Montevideo")
        # Las mantenemos pero las marcamos
        df["coord_valid"] = valid_mask
    else:
        df["coord_valid"] = True
    
    # Determinar estado del contenedor
    motivo_col = _find_column(df, ["motivo", "motivo_inactividad", "estado"])
    if motivo_col:
        df["status"] = df[motivo_col].apply(
            lambda x: "inactive" if pd.notna(x) and str(x).strip() != "" else "active"
        )
    else:
        df["status"] = "active"
    
    # Construir DataFrame de salida
    circuit_col = _find_column(df, ["cod_circuito", "circuito", "circuit"])
    schedule_col = _find_column(df, ["turno_horario", "turno", "horario", "frecuencia"])
    
    output = pd.DataFrame({
        "container_id": df["gid"].astype(str),
        "circuit_id": df[circuit_col].astype(str) if circuit_col else "UNKNOWN",
        "schedule": df[schedule_col].astype(str) if schedule_col else "",
        "status": df["status"],
        "x_utm": df["x"].round(2),
        "y_utm": df["y"].round(2),
        "latitude": df["latitude"].round(6),
        "longitude": df["longitude"].round(6),
        "coord_valid": df["coord_valid"],
    })
    
    # Parsear turno y días del schedule
    if schedule_col:
        output = _parse_schedule(output)
    
    # Guardar
    output_path = os.path.join(output_dir, "containers_wgs84.csv")
    output.to_csv(output_path, index=False)
    
    # Estadísticas
    active_count = (output["status"] == "active").sum()
    circuits = output["circuit_id"].nunique()
    
    print(f"\n   ✅ Contenedores procesados: {len(output)}")
    print(f"   ✅ Contenedores activos: {active_count}")
    print(f"   ✅ Circuitos únicos: {circuits}")
    print(f"   ✅ Guardado en: {output_path}")
    
    # Guardar también un resumen por circuito
    _save_circuit_summary(output, output_dir)
    
    # Guardar ubicaciones fijas
    _save_fixed_locations(output_dir)
    
    return output


def _parse_schedule(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extrae turno (M/V/N = matutino/vespertino/nocturno) y días de la semana
    del campo schedule. Ejemplo de formatos típicos:
        "LU-MA-MI-JU-VI-SA M"  → días: LU,MA,MI,JU,VI,SA  turno: M
        "LU-MI-VI N"           → días: LU,MI,VI  turno: N
    """
    def extract_shift(val):
        if not isinstance(val, str) or val.strip() == "":
            return "UNKNOWN"
        parts = val.strip().split()
        if len(parts) >= 2:
            shift_char = parts[-1].upper()
            if shift_char in ("M", "V", "N"):
                return {"M": "morning", "V": "afternoon", "N": "night"}.get(shift_char, shift_char)
        return "UNKNOWN"
    
    def extract_days(val):
        if not isinstance(val, str) or val.strip() == "":
            return ""
        parts = val.strip().split()
        if len(parts) >= 1:
            return parts[0]  # "LU-MA-MI-JU-VI-SA"
        return ""
    
    df["shift"] = df["schedule"].apply(extract_shift)
    df["collection_days"] = df["schedule"].apply(extract_days)
    
    return df


# ─────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────

def _read_csv_flexible(path: str) -> pd.DataFrame:
    """Intenta leer un CSV con distintos separadores y encodings."""
    encodings = ["utf-8", "latin-1", "cp1252"]
    separators = [",", ";", "\t"]
    
    for enc in encodings:
        for sep in separators:
            try:
                df = pd.read_csv(path, sep=sep, encoding=enc, dtype=str)
                # Verificar que tenemos más de 1 columna (si no, el separador es incorrecto)
                if len(df.columns) > 1:
                    print(f"   Encoding: {enc}, Separador: {repr(sep)}")
                    return df
            except Exception:
                continue
    
    raise ValueError(
        f"No se pudo leer {path}. Intentados encodings {encodings} y separadores {separators}."
    )


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """Busca una columna en el DataFrame probando varios nombres posibles."""
    for name in candidates:
        if name in df.columns:
            return name
    return None


def _try_remap_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Intenta mapear nombres de columnas alternativos a los esperados."""
    mappings = {
        "gid": ["id", "container_id", "nro", "numero", "cod_contenedor"],
        "x": ["coord_x", "easting", "este", "utm_x", "xcoord"],
        "y": ["coord_y", "northing", "norte", "utm_y", "ycoord"],
        "cod_circuito": ["circuito", "circuit", "cod_circ"],
        "turno_horario": ["turno", "horario", "frecuencia", "schedule"],
        "motivo": ["motivo_inactividad", "estado", "state"],
    }
    
    rename_map = {}
    for target, alternatives in mappings.items():
        if target not in df.columns:
            for alt in alternatives:
                if alt in df.columns:
                    rename_map[alt] = target
                    break
    
    if rename_map:
        print(f"   Renombrando: {rename_map}")
        df = df.rename(columns=rename_map)
    
    return df


def _save_circuit_summary(df: pd.DataFrame, output_dir: str):
    """Genera un resumen por circuito: cantidad de contenedores, centroide, turno."""
    active = df[df["status"] == "active"]
    
    summary = active.groupby("circuit_id").agg(
        container_count=("container_id", "count"),
        centroid_lat=("latitude", "mean"),
        centroid_lon=("longitude", "mean"),
        lat_min=("latitude", "min"),
        lat_max=("latitude", "max"),
        lon_min=("longitude", "min"),
        lon_max=("longitude", "max"),
        shift=("shift", lambda x: x.mode()[0] if hasattr(x, "mode") and len(x.mode()) > 0 else "UNKNOWN"),
    ).reset_index()
    
    summary["centroid_lat"] = summary["centroid_lat"].round(6)
    summary["centroid_lon"] = summary["centroid_lon"].round(6)
    
    output_path = os.path.join(output_dir, "circuits_summary.csv")
    summary.to_csv(output_path, index=False)
    print(f"   ✅ Resumen de circuitos: {output_path} ({len(summary)} circuitos)")


def _save_fixed_locations(output_dir: str):
    """Guarda las ubicaciones fijas (depots, sitio disposición final, etc.)."""
    output_path = os.path.join(output_dir, "fixed_locations.json")
    with open(output_path, "w") as f:
        json.dump(FIXED_LOCATIONS, f, indent=2, ensure_ascii=False)
    print(f"   ✅ Ubicaciones fijas: {output_path}")


# ─────────────────────────────────────────────────────────
# Procesamiento de circuitos
# ─────────────────────────────────────────────────────────

def process_circuits(input_path: str, output_dir: str) -> pd.DataFrame:
    """
    Lee el CSV de circuitos de recolección y genera un CSV limpio con polígonos en WGS84.

    Estructura del CSV de entrada (campos según Circuitos_recoleccion.txt):
        gid              - Clave del polígono
        cod_circuito     - Código del circuito de recolección
        municipio        - Nombre del Municipio (A-G)
        vertices         - Coordenadas de los puntos del polígono (UTM 21S)

    Un circuito puede tener varios polígonos (varias filas con el mismo cod_circuito).

    CSV de salida (circuits_wgs84.csv):
        gid, circuit_id, municipality, vertices_wgs84
        
    También genera circuits_centroids.csv con un centroide por circuito.
    """
    print(f"\n🔷 Procesando circuitos: {input_path}")

    df = _read_csv_flexible(input_path)
    print(f"   Filas leídas: {len(df)}")
    print(f"   Columnas: {list(df.columns)}")

    df.columns = [c.strip().lower() for c in df.columns]

    # Mapear nombres de columnas
    col_gid = _find_column(df, ["gid", "id"]) or "gid"
    col_circuit = _find_column(df, ["cod_circuito", "circuito", "cod_circ"]) or "cod_circuito"
    col_municipio = _find_column(df, ["municipio", "municipality", "mun"]) or "municipio"
    col_vertices = _find_column(df, ["vertices", "geometry", "geom", "wkt", "the_geom"]) or "vertices"

    if col_vertices not in df.columns:
        print(f"   ⚠️  No se encontró columna de vértices. Columnas disponibles: {list(df.columns)}")
        print(f"   Generando archivo solo con metadatos de circuitos (sin polígonos)...")
        return _process_circuits_without_vertices(df, col_gid, col_circuit, col_municipio, output_dir)

    transformer = create_transformer()
    print("   🔄 Convirtiendo vértices UTM 21S → WGS84...")

    rows_out = []
    for _, row in df.iterrows():
        raw_vertices = str(row.get(col_vertices, ""))
        converted = _convert_vertices(raw_vertices, transformer)

        rows_out.append({
            "gid": row.get(col_gid, ""),
            "circuit_id": row.get(col_circuit, ""),
            "municipality": row.get(col_municipio, ""),
            "vertices_wgs84": converted["wgs84_string"],
            "vertex_count": converted["count"],
            "centroid_lat": converted["centroid_lat"],
            "centroid_lon": converted["centroid_lon"],
        })

    output_df = pd.DataFrame(rows_out)

    # Guardar polígonos completos
    output_path = os.path.join(output_dir, "circuits_wgs84.csv")
    output_df.to_csv(output_path, index=False)
    print(f"   ✅ Circuitos con polígonos: {output_path} ({len(output_df)} filas)")

    # Generar centroides agrupados por circuito (un circuito puede tener varios polígonos)
    _save_circuit_centroids(output_df, output_dir)

    # Guardar como GeoJSON para visualización rápida en geojson.io / Leaflet
    _save_circuits_geojson(output_df, output_dir)

    return output_df


def _convert_vertices(raw: str, transformer: Transformer) -> dict:
    """
    Convierte una cadena de vértices UTM a WGS84.

    Los vértices pueden venir en varios formatos:
      - "x1 y1, x2 y2, x3 y3, ..."
      - "x1,y1;x2,y2;x3,y3;..."
      - "POLYGON((x1 y1, x2 y2, ...))"
      - "(x1 y1) (x2 y2) ..."
    """
    if not raw or raw.strip() in ("", "nan", "None"):
        return {"wgs84_string": "", "count": 0, "centroid_lat": None, "centroid_lon": None}

    # Limpiar formato WKT si existe
    clean = raw.strip()
    for prefix in ["POLYGON((", "POLYGON ((", "MULTIPOLYGON(((", "MULTIPOLYGON (((", "LINESTRING(", "LINESTRING ("]:
        if clean.upper().startswith(prefix.upper()):
            clean = clean[len(prefix):]
            break
    clean = clean.rstrip(")").strip()

    # Intentar parsear las coordenadas
    pairs = []

    # Formato "x1 y1, x2 y2, ..." (el más común para la Intendencia)
    if "," in clean and " " in clean:
        for pair_str in clean.split(","):
            parts = pair_str.strip().split()
            if len(parts) >= 2:
                try:
                    x, y = float(parts[0]), float(parts[1])
                    pairs.append((x, y))
                except ValueError:
                    continue

    # Formato "x1;y1;x2;y2;..." (pares alternados separados por ;)
    elif ";" in clean and " " not in clean.replace(";", "").replace(".", "").replace("-", "").strip("0123456789"):
        values = clean.split(";")
        for i in range(0, len(values) - 1, 2):
            try:
                x, y = float(values[i]), float(values[i + 1])
                pairs.append((x, y))
            except (ValueError, IndexError):
                continue

    # Formato genérico: intentar extraer todos los números y emparejarlos
    if not pairs:
        import re
        numbers = re.findall(r"[-+]?\d*\.?\d+", clean)
        for i in range(0, len(numbers) - 1, 2):
            try:
                x, y = float(numbers[i]), float(numbers[i + 1])
                # Verificar que parecen ser coordenadas UTM (no lat/lon)
                if x > 100000 and y > 1000000:
                    pairs.append((x, y))
            except ValueError:
                continue

    if not pairs:
        return {"wgs84_string": "", "count": 0, "centroid_lat": None, "centroid_lon": None}

    # Convertir cada par
    converted = []
    for x, y in pairs:
        try:
            lat, lon = utm_to_wgs84(transformer, x, y)
            if is_valid_montevideo_coord(lat, lon):
                converted.append((lat, lon))
        except Exception:
            continue

    if not converted:
        return {"wgs84_string": "", "count": 0, "centroid_lat": None, "centroid_lon": None}

    # Centroide simple (promedio de vértices)
    avg_lat = sum(c[0] for c in converted) / len(converted)
    avg_lon = sum(c[1] for c in converted) / len(converted)

    # Formato de salida: "lat1 lon1, lat2 lon2, ..."
    wgs84_str = ", ".join(f"{lat:.6f} {lon:.6f}" for lat, lon in converted)

    return {
        "wgs84_string": wgs84_str,
        "count": len(converted),
        "centroid_lat": round(avg_lat, 6),
        "centroid_lon": round(avg_lon, 6),
    }


def _process_circuits_without_vertices(
    df: pd.DataFrame, col_gid: str, col_circuit: str, col_municipio: str, output_dir: str
) -> pd.DataFrame:
    """Procesa circuitos cuando no hay columna de vértices (solo metadatos)."""
    output = pd.DataFrame({
        "gid": df[col_gid] if col_gid in df.columns else "",
        "circuit_id": df[col_circuit] if col_circuit in df.columns else "",
        "municipality": df[col_municipio] if col_municipio in df.columns else "",
    })

    output_path = os.path.join(output_dir, "circuits_metadata.csv")
    output.to_csv(output_path, index=False)
    print(f"   ✅ Metadatos de circuitos: {output_path} ({len(output)} filas)")

    return output


def _save_circuit_centroids(df: pd.DataFrame, output_dir: str):
    """Genera un CSV con un centroide por circuito único."""
    valid = df.dropna(subset=["centroid_lat", "centroid_lon"])
    if valid.empty:
        return

    centroids = valid.groupby("circuit_id").agg(
        municipality=("municipality", "first"),
        centroid_lat=("centroid_lat", "mean"),
        centroid_lon=("centroid_lon", "mean"),
        polygon_count=("gid", "count"),
        total_vertices=("vertex_count", "sum"),
    ).reset_index()

    centroids["centroid_lat"] = centroids["centroid_lat"].round(6)
    centroids["centroid_lon"] = centroids["centroid_lon"].round(6)

    output_path = os.path.join(output_dir, "circuits_centroids.csv")
    centroids.to_csv(output_path, index=False)
    print(f"   ✅ Centroides de circuitos: {output_path} ({len(centroids)} circuitos únicos)")


def _save_circuits_geojson(df: pd.DataFrame, output_dir: str):
    """
    Genera un archivo GeoJSON con los polígonos de circuitos.
    Se puede abrir directamente en geojson.io o Leaflet para verificación visual.
    """
    features = []

    for _, row in df.iterrows():
        verts_str = row.get("vertices_wgs84", "")
        if not verts_str or str(verts_str).strip() == "":
            continue

        # Parsear "lat1 lon1, lat2 lon2, ..." a [[lon1, lat1], [lon2, lat2], ...]
        coords = []
        for pair in str(verts_str).split(","):
            parts = pair.strip().split()
            if len(parts) == 2:
                try:
                    lat, lon = float(parts[0]), float(parts[1])
                    coords.append([lon, lat])  # GeoJSON usa [lon, lat]
                except ValueError:
                    continue

        if len(coords) < 3:
            continue

        # Cerrar el polígono si no está cerrado
        if coords[0] != coords[-1]:
            coords.append(coords[0])

        feature = {
            "type": "Feature",
            "properties": {
                "gid": str(row.get("gid", "")),
                "circuit_id": str(row.get("circuit_id", "")),
                "municipality": str(row.get("municipality", "")),
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [coords],
            },
        }
        features.append(feature)

    if not features:
        print("   ⚠️  No se generaron features GeoJSON (sin vértices válidos)")
        return

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    output_path = os.path.join(output_dir, "circuits.geojson")
    with open(output_path, "w") as f:
        json.dump(geojson, f, indent=2)
    print(f"   ✅ GeoJSON de circuitos: {output_path} ({len(features)} polígonos)")
    print(f"      Podés verificarlo en: https://geojson.io")


# ─────────────────────────────────────────────────────────
# Reportes de validación
# ─────────────────────────────────────────────────────────

def print_sample(df: pd.DataFrame, n: int = 5):
    """Imprime una muestra de los datos convertidos para verificación manual."""
    print(f"\n📋 Muestra de {n} contenedores convertidos:")
    print("-" * 90)
    sample = df.head(n)
    for _, row in sample.iterrows():
        print(
            f"   ID: {row['container_id']:>8s} | "
            f"Circuito: {row['circuit_id']:>6s} | "
            f"UTM: ({row['x_utm']:>10.2f}, {row['y_utm']:>10.2f}) → "
            f"WGS84: ({row['latitude']:>10.6f}, {row['longitude']:>11.6f}) | "
            f"{row['status']}"
        )
    print("-" * 90)
    print(
        f"   🔗 Verificá en Google Maps: "
        f"https://www.google.com/maps/@{sample.iloc[0]['latitude']},{sample.iloc[0]['longitude']},15z"
    )


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convierte CSV de contenedores de Montevideo de UTM 21S a WGS84",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplo:
  python convert_coordinates.py --containers data/raw/Contenedores_domiciliarios.csv --output data/processed/
  python convert_coordinates.py --containers data/raw/Contenedores_domiciliarios.csv --circuits data/raw/Circuitos_recoleccion.csv --output data/processed/
        """,
    )
    parser.add_argument(
        "--containers",
        required=True,
        help="Path al CSV de contenedores domiciliarios (de catalogodatos.gub.uy)",
    )
    parser.add_argument(
        "--circuits",
        required=False,
        help="Path al CSV de circuitos de recolección (de catalogodatos.gub.uy)",
    )
    parser.add_argument(
        "--output",
        default="processed/",
        help="Directorio de salida para los CSV procesados (default: processed/)",
    )

    args = parser.parse_args()

    # Validar inputs
    if not os.path.exists(args.containers):
        print(f"❌ Archivo no encontrado: {args.containers}")
        sys.exit(1)

    if args.circuits and not os.path.exists(args.circuits):
        print(f"❌ Archivo no encontrado: {args.circuits}")
        sys.exit(1)

    # Crear directorio de salida
    os.makedirs(args.output, exist_ok=True)

    print("=" * 60)
    print("  SmartWaste MVD — Conversión de Coordenadas")
    print("  UTM 21S (SIRGAS2000) → WGS84")
    print("=" * 60)

    # Procesar contenedores
    df = process_containers(args.containers, args.output)
    print_sample(df)

    # Procesar circuitos (si se proporcionan)
    if args.circuits:
        process_circuits(args.circuits, args.output)

    print(f"\n{'=' * 60}")
    print(f"  ✅ Proceso completado. Archivos generados en: {args.output}/")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()