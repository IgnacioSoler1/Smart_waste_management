#!/usr/bin/env python3
"""
validate_data.py — SmartWaste MVD

Lee los archivos JSON enriquecidos generados por consolidate_data.py y valida
su consistencia, imprimiendo un reporte detallado con errores y advertencias.

Validaciones:
  1. Contenedores sin circuit_id reconocido
  2. Contenedores con coordenadas fuera del bounding box de Montevideo
  3. Circuitos sin contenedores activos
  4. Circuitos con depot_id inválido
  5. Contenedores con zone/depot_id inconsistente con su longitud
  6. Contenedores con shift=UNKNOWN
  7. Contenedores con collection_days vacío
  8. Circuitos con turno UNKNOWN y contenedores activos

Uso:
  python data/scripts/validate_data.py
  python data/scripts/validate_data.py --processed-dir data/processed/

Dependencias: json, pathlib, argparse (stdlib únicamente)
"""

import argparse
import json
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────
# Constantes del dominio
# ─────────────────────────────────────────────────────────

MVD_BOUNDS: dict[str, float] = {
    "lat_min": -34.95,
    "lat_max": -34.70,
    "lon_min": -56.40,
    "lon_max": -55.95,
}

EAST_WEST_BOUNDARY_LON: float = -56.17

VALID_DEPOT_IDS: set[str] = {
    "depot_estacion_transferencia",
    "depot_felipe_cardoso",
}

VALID_ZONES: set[str] = {"east", "west"}

VALID_SHIFTS: set[str] = {"morning", "afternoon", "night", "UNKNOWN"}


# ─────────────────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────────────────

def load_json(path: Path) -> dict | list:
    if not path.exists():
        print(f"❌ Archivo no encontrado: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────
# Validaciones
# ─────────────────────────────────────────────────────────

class ValidationReport:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def info_msg(self, msg: str) -> None:
        self.info.append(msg)

    @property
    def has_issues(self) -> bool:
        return bool(self.errors or self.warnings)


def validate_circuits(circuits: dict[str, dict], report: ValidationReport) -> None:
    """Valida el diccionario de circuitos enriquecidos."""
    total = len(circuits)
    empty_circuits: list[str] = []
    invalid_depot: list[str] = []
    invalid_zone: list[str] = []
    unknown_shift_with_containers: list[str] = []
    outside_mvd: list[str] = []

    for cid, circuit in circuits.items():
        # 1. Circuito sin contenedores activos
        if circuit.get("container_count", 0) == 0:
            empty_circuits.append(cid)

        # 2. Depot inválido
        depot_id = circuit.get("depot", {}).get("id", "")
        if depot_id not in VALID_DEPOT_IDS:
            invalid_depot.append(f"{cid} (depot_id={depot_id!r})")

        # 3. Zona inválida
        zone = circuit.get("zone", "")
        if zone not in VALID_ZONES:
            invalid_zone.append(f"{cid} (zone={zone!r})")

        # 4. Consistencia zona ↔ longitud
        centroid_lon = circuit.get("centroid_lon")
        if centroid_lon is not None:
            expected_zone = "west" if centroid_lon < EAST_WEST_BOUNDARY_LON else "east"
            if zone in VALID_ZONES and zone != expected_zone:
                report.error(
                    f"Circuito {cid}: zona={zone!r} pero centroid_lon={centroid_lon:.4f} "
                    f"implica zona={expected_zone!r}"
                )

        # 5. Turno UNKNOWN con contenedores activos
        if circuit.get("dominant_shift") == "UNKNOWN" and circuit.get("container_count", 0) > 0:
            unknown_shift_with_containers.append(cid)

        # 6. Centroide fuera de Montevideo
        centroid_lat = circuit.get("centroid_lat")
        if centroid_lat is not None and centroid_lon is not None:
            if not (
                MVD_BOUNDS["lat_min"] <= centroid_lat <= MVD_BOUNDS["lat_max"]
                and MVD_BOUNDS["lon_min"] <= centroid_lon <= MVD_BOUNDS["lon_max"]
            ):
                outside_mvd.append(
                    f"{cid} ({centroid_lat:.4f}, {centroid_lon:.4f})"
                )

    # Reportar
    report.info_msg(f"Circuitos totales:                  {total:>6}")
    report.info_msg(f"  Con contenedores activos:         {total - len(empty_circuits):>6}")
    report.info_msg(f"  Sin contenedores activos:         {len(empty_circuits):>6}")
    report.info_msg(
        f"  Turno UNKNOWN (con contenedores): {len(unknown_shift_with_containers):>6}"
    )

    if invalid_depot:
        for msg in invalid_depot:
            report.error(f"Circuito con depot_id inválido: {msg}")

    if invalid_zone:
        for msg in invalid_zone:
            report.error(f"Circuito con zona inválida: {msg}")

    if outside_mvd:
        for msg in outside_mvd[:10]:
            report.error(f"Centroide de circuito fuera de Montevideo: {msg}")
        if len(outside_mvd) > 10:
            report.error(f"  ... y {len(outside_mvd) - 10} circuito(s) más")

    if empty_circuits:
        # Mostrar hasta 10 circuitos vacíos
        sample = empty_circuits[:10]
        extra = len(empty_circuits) - len(sample)
        report.warning(
            f"Circuitos sin contenedores activos ({len(empty_circuits)}): "
            + ", ".join(sample)
            + (f" ... (+{extra} más)" if extra else "")
        )

    if unknown_shift_with_containers:
        sample = unknown_shift_with_containers[:10]
        extra = len(unknown_shift_with_containers) - len(sample)
        report.warning(
            f"Circuitos con turno UNKNOWN y contenedores activos "
            f"({len(unknown_shift_with_containers)}): "
            + ", ".join(sample)
            + (f" ... (+{extra} más)" if extra else "")
        )


def validate_containers(
    containers: list[dict],
    circuits: dict[str, dict],
    report: ValidationReport,
) -> None:
    """Valida la lista de contenedores enriquecidos."""
    total = len(containers)
    unknown_circuit: list[str] = []
    outside_mvd: list[str] = []
    invalid_depot: list[str] = []
    invalid_zone: list[str] = []
    zone_mismatch: list[str] = []
    unknown_shift: list[str] = []
    no_days: list[str] = []
    duplicate_ids: list[str] = []

    seen_ids: set[str] = set()

    for container in containers:
        cid_container = container.get("container_id", "?")

        # IDs duplicados
        if cid_container in seen_ids:
            duplicate_ids.append(cid_container)
        seen_ids.add(cid_container)

        # 1. Contenedor sin circuito reconocido
        circuit_id = container.get("circuit_id", "")
        if circuit_id not in circuits:
            unknown_circuit.append(f"{cid_container} (circuit_id={circuit_id!r})")

        # 2. Coordenadas fuera de Montevideo
        lat = container.get("latitude")
        lon = container.get("longitude")
        if lat is not None and lon is not None:
            if not (
                MVD_BOUNDS["lat_min"] <= lat <= MVD_BOUNDS["lat_max"]
                and MVD_BOUNDS["lon_min"] <= lon <= MVD_BOUNDS["lon_max"]
            ):
                outside_mvd.append(f"{cid_container} ({lat:.4f}, {lon:.4f})")

        # 3. Depot inválido
        depot_id = container.get("depot_id", "")
        if depot_id not in VALID_DEPOT_IDS:
            invalid_depot.append(f"{cid_container} (depot_id={depot_id!r})")

        # 4. Zona inválida
        zone = container.get("zone", "")
        if zone not in VALID_ZONES:
            invalid_zone.append(f"{cid_container} (zone={zone!r})")

        # 5. Consistencia zona ↔ longitud del contenedor
        if lon is not None and zone in VALID_ZONES:
            expected_zone = "west" if lon < EAST_WEST_BOUNDARY_LON else "east"
            if zone != expected_zone:
                zone_mismatch.append(
                    f"{cid_container} (zone={zone!r}, lon={lon:.4f} → esperado {expected_zone!r})"
                )

        # 6. Shift desconocido
        shift = container.get("shift", "UNKNOWN")
        if shift == "UNKNOWN":
            unknown_shift.append(cid_container)

        # 7. Sin días de recolección
        if not container.get("collection_days"):
            no_days.append(cid_container)

    # Estadísticas generales
    zones: dict[str, int] = {}
    shifts: dict[str, int] = {}
    for c in containers:
        zones[c.get("zone", "UNKNOWN")] = zones.get(c.get("zone", "UNKNOWN"), 0) + 1
        shifts[c.get("shift", "UNKNOWN")] = shifts.get(c.get("shift", "UNKNOWN"), 0) + 1

    report.info_msg(f"Contenedores totales (activos):     {total:>6}")
    for zone_name in sorted(zones):
        report.info_msg(f"  Zona {zone_name:<8}                {zones[zone_name]:>6}")
    report.info_msg("  Por turno:")
    for shift_name in sorted(shifts):
        report.info_msg(f"    {shift_name:<14}             {shifts[shift_name]:>6}")
    report.info_msg(f"  Sin días de recolección:          {len(no_days):>6}")
    report.info_msg(f"  Turno UNKNOWN:                    {len(unknown_shift):>6}")

    # Errores
    if duplicate_ids:
        for dup in duplicate_ids[:10]:
            report.error(f"container_id duplicado: {dup}")
        if len(duplicate_ids) > 10:
            report.error(f"  ... y {len(duplicate_ids) - 10} duplicado(s) más")

    if unknown_circuit:
        for msg in unknown_circuit[:10]:
            report.error(f"Contenedor con circuit_id desconocido: {msg}")
        if len(unknown_circuit) > 10:
            report.error(f"  ... y {len(unknown_circuit) - 10} contenedor(es) más")

    if outside_mvd:
        for msg in outside_mvd[:10]:
            report.error(f"Contenedor fuera de Montevideo: {msg}")
        if len(outside_mvd) > 10:
            report.error(f"  ... y {len(outside_mvd) - 10} contenedor(es) más")

    if invalid_depot:
        for msg in invalid_depot[:10]:
            report.error(f"Contenedor con depot_id inválido: {msg}")

    if invalid_zone:
        for msg in invalid_zone[:10]:
            report.error(f"Contenedor con zona inválida: {msg}")

    if zone_mismatch:
        for msg in zone_mismatch[:10]:
            report.warning(f"Zona del contenedor no coincide con su longitud: {msg}")
        if len(zone_mismatch) > 10:
            report.warning(f"  ... y {len(zone_mismatch) - 10} contenedor(es) más")

    if unknown_shift:
        sample = unknown_shift[:5]
        extra = len(unknown_shift) - len(sample)
        report.warning(
            f"Contenedores con turno UNKNOWN ({len(unknown_shift)}): "
            + ", ".join(sample)
            + (f" ... (+{extra} más)" if extra else "")
        )

    if no_days:
        sample = no_days[:5]
        extra = len(no_days) - len(sample)
        report.warning(
            f"Contenedores sin días de recolección ({len(no_days)}): "
            + ", ".join(sample)
            + (f" ... (+{extra} más)" if extra else "")
        )


def validate_cross(
    circuits: dict[str, dict],
    containers: list[dict],
    report: ValidationReport,
) -> None:
    """Validaciones cruzadas entre circuitos y contenedores."""
    # Recalcular conteo real de contenedores por circuito
    real_counts: dict[str, int] = {}
    for c in containers:
        cid = c.get("circuit_id", "")
        real_counts[cid] = real_counts.get(cid, 0) + 1

    count_mismatches: list[str] = []
    for cid, circuit in circuits.items():
        stored = circuit.get("container_count", 0)
        real = real_counts.get(cid, 0)
        if stored != real:
            count_mismatches.append(
                f"{cid}: circuits_enriched dice {stored}, "
                f"containers_enriched tiene {real}"
            )

    if count_mismatches:
        for msg in count_mismatches[:10]:
            report.error(f"Discrepancia en container_count: {msg}")
        if len(count_mismatches) > 10:
            report.error(f"  ... y {len(count_mismatches) - 10} discrepancia(s) más")
    else:
        report.info_msg("  Conteo de contenedores consistente entre archivos.      ✓")


# ─────────────────────────────────────────────────────────
# Impresión del reporte
# ─────────────────────────────────────────────────────────

def print_report(report: ValidationReport) -> None:
    print()
    print("─" * 60)
    print("  Información general")
    print("─" * 60)
    for msg in report.info:
        print(f"  {msg}")

    if report.errors:
        print()
        print("─" * 60)
        print(f"  ERRORES ({len(report.errors)})")
        print("─" * 60)
        for msg in report.errors:
            print(f"  ✗ {msg}")

    if report.warnings:
        print()
        print("─" * 60)
        print(f"  ADVERTENCIAS ({len(report.warnings)})")
        print("─" * 60)
        for msg in report.warnings:
            print(f"  ⚠ {msg}")

    print()
    print("─" * 60)
    if not report.errors and not report.warnings:
        print("  ✅ Validación completada sin errores ni advertencias.")
    elif not report.errors:
        print(
            f"  ⚠  Validación completada con {len(report.warnings)} advertencia(s). "
            "Sin errores críticos."
        )
    else:
        print(
            f"  ❌ Validación completada con {len(report.errors)} error(es) y "
            f"{len(report.warnings)} advertencia(s)."
        )
    print("─" * 60)


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Valida consistencia de los archivos JSON enriquecidos de SmartWaste MVD.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplo:
  python data/scripts/validate_data.py
  python data/scripts/validate_data.py --processed-dir data/processed/
        """,
    )
    parser.add_argument(
        "--processed-dir",
        default="data/processed",
        help="Directorio con los JSON enriquecidos (default: data/processed/)",
    )
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)

    print("=" * 60)
    print("  SmartWaste MVD — Validación de datos")
    print("=" * 60)

    circuits_path = processed_dir / "circuits_enriched.json"
    containers_path = processed_dir / "containers_enriched.json"

    print(f"\n  Leyendo {circuits_path.name}...")
    circuits_raw = load_json(circuits_path)
    # Puede ser lista o dict — normalizar a dict keyed by circuit_id
    if isinstance(circuits_raw, list):
        circuits: dict[str, dict] = {c["circuit_id"]: c for c in circuits_raw}
    else:
        circuits = circuits_raw  # type: ignore[assignment]

    print(f"  Leyendo {containers_path.name}...")
    containers: list[dict] = load_json(containers_path)  # type: ignore[assignment]

    print(f"\n  circuits_enriched:   {len(circuits):,} circuitos")
    print(f"  containers_enriched: {len(containers):,} contenedores")

    report = ValidationReport()

    print("\n  Validando circuitos...")
    validate_circuits(circuits, report)

    print("  Validando contenedores...")
    validate_containers(containers, circuits, report)

    print("  Validando consistencia cruzada...")
    validate_cross(circuits, containers, report)

    print_report(report)

    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
