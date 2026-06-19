"""
benchmark_osrm.py — SmartWaste MVD Performance Tests

Mide la latencia del endpoint OSRM `/table/v1/driving/...` cuando se le pide
una matriz de duración + distancia de N×N ubicaciones. Cubre el rango completo
de tamaños que produce el route-optimizer consolidado:
    - 82  → un circuito promedio de Montevideo
    - 200 → un circuito grande (tail de la distribución)
    - 500 → un grupo consolidado típico (≈6 circuitos)
    - 1000 → el tope del límite de cuOpt cloud API
    - 2000 → futuro multi-municipio en self-hosted cuOpt

Las ubicaciones se muestrean del CSV real de contenedores de Montevideo
para que las matrices sean representativas (densidad urbana real, no
distribución uniforme).

Uso:
    # Requiere OSRM corriendo en localhost:5000
    cd osrm && docker compose up -d osrm-server

    python benchmark_osrm.py                  # full
    python benchmark_osrm.py --quick          # 82, 200 nodos x 3 reps
    python benchmark_osrm.py --base-url URL   # OSRM remoto
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Imports del proyecto
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from osrm_client import OSRMClient, OSRMError  # noqa: E402

from problem_generator import ProblemGenerator  # noqa: E402

logger = logging.getLogger(__name__)


# Tamaños cubiertos por el benchmark (incluyendo depot)
DEFAULT_SIZES = [82, 200, 500, 1000, 2000]
QUICK_SIZES = [82, 200]

DEFAULT_REPETITIONS = 5
QUICK_REPETITIONS = 3

CONTAINERS_CSV = (
    Path(__file__).resolve().parents[2]
    / "data" / "processed" / "containers_wgs84.csv"
)


@dataclass
class SizeResult:
    size: int
    repetitions: int
    samples_ms: list[float]
    elements: int  # n*n

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.samples_ms)

    @property
    def median_ms(self) -> float:
        return statistics.median(self.samples_ms)

    @property
    def stdev_ms(self) -> float:
        return statistics.stdev(self.samples_ms) if len(self.samples_ms) > 1 else 0.0

    @property
    def p95_ms(self) -> float:
        if len(self.samples_ms) < 2:
            return self.samples_ms[0]
        sorted_samples = sorted(self.samples_ms)
        idx = max(0, int(round(0.95 * (len(sorted_samples) - 1))))
        return sorted_samples[idx]

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "elements": self.elements,
            "repetitions": self.repetitions,
            "samples_ms": [round(s, 1) for s in self.samples_ms],
            "mean_ms": round(self.mean_ms, 1),
            "median_ms": round(self.median_ms, 1),
            "p95_ms": round(self.p95_ms, 1),
            "stdev_ms": round(self.stdev_ms, 1),
        }


def _sample_locations(generator: ProblemGenerator, n: int, seed: int) -> list[tuple[float, float]]:
    """
    Muestrea N coordenadas (lat, lon) del pool de contenedores. Reutiliza el
    mismo seed para que las repeticiones del mismo tamaño usen exactamente las
    mismas ubicaciones (medimos varianza de OSRM, no de la geometría).
    """
    rng = random.Random(seed)
    pool = list(generator._containers)
    if n > len(pool):
        # Si pedimos más de lo que hay, repetimos con jitter pequeño.
        base = pool
        extras = n - len(base)
        sampled = list(base)
        for _ in range(extras):
            lat, lon = rng.choice(pool)
            jitter_lat = lat + rng.uniform(-1e-4, 1e-4)
            jitter_lon = lon + rng.uniform(-1e-4, 1e-4)
            sampled.append((jitter_lat, jitter_lon))
        rng.shuffle(sampled)
        return sampled
    return rng.sample(pool, n)


def _run_one(client: OSRMClient, locations: list[tuple[float, float]]) -> float:
    """Devuelve la latencia de OSRM en milisegundos para una matriz N×N."""
    start = time.perf_counter()
    client.get_distance_matrix(locations)
    return (time.perf_counter() - start) * 1000.0


def run_benchmark(
    base_url: str,
    sizes: list[int],
    repetitions: int,
    seed: int = 42,
    warmup: bool = True,
) -> list[SizeResult]:
    if not CONTAINERS_CSV.exists():
        raise FileNotFoundError(f"No se encontró el CSV de contenedores en {CONTAINERS_CSV}")

    generator = ProblemGenerator(str(CONTAINERS_CSV))
    client = OSRMClient(base_url=base_url, timeout=120.0)

    # Warm-up: OSRM cachea metadata interna en la primera request.
    if warmup:
        logger.info("Warm-up con matriz 10×10...")
        warm_locs = _sample_locations(generator, 10, seed=999)
        client.get_distance_matrix(warm_locs)

    results: list[SizeResult] = []
    for size in sizes:
        logger.info("Tamaño N=%d (%d×%d = %d elementos)", size, size, size, size * size)
        locations = _sample_locations(generator, size, seed=seed + size)
        samples: list[float] = []
        for rep in range(repetitions):
            try:
                ms = _run_one(client, locations)
            except (OSRMError, Exception) as exc:  # noqa: BLE001
                logger.error("Falla en N=%d rep=%d: %s", size, rep, exc)
                continue
            samples.append(ms)
            logger.info("  rep %d/%d → %.1f ms", rep + 1, repetitions, ms)
        if not samples:
            logger.warning("No se pudieron tomar muestras para N=%d", size)
            continue
        results.append(
            SizeResult(
                size=size,
                repetitions=len(samples),
                samples_ms=samples,
                elements=size * size,
            )
        )
    return results


def print_table(results: list[SizeResult]) -> None:
    print()
    print(f"{'N':>5} {'Elements':>10} {'reps':>5} "
          f"{'mean (ms)':>12} {'median (ms)':>13} "
          f"{'p95 (ms)':>10} {'stdev (ms)':>11}")
    print("-" * 70)
    for r in results:
        print(f"{r.size:>5d} {r.elements:>10d} {r.repetitions:>5d} "
              f"{r.mean_ms:>12.1f} {r.median_ms:>13.1f} "
              f"{r.p95_ms:>10.1f} {r.stdev_ms:>11.1f}")
    print()


def save_json(results: list[SizeResult], path: Path, meta: dict[str, Any]) -> None:
    payload = {
        "meta": meta,
        "results": [r.to_dict() for r in results],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info("Resultados guardados en %s", path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark de latencia OSRM Table API")
    parser.add_argument("--base-url", default="http://localhost:5000",
                        help="URL base del servidor OSRM (default: localhost:5000)")
    parser.add_argument("--quick", action="store_true",
                        help="Tamaños y reps reducidos (debug rápido)")
    parser.add_argument("--sizes", type=str, default=None,
                        help="Lista de tamaños separados por coma (sobreescribe defaults)")
    parser.add_argument("--reps", type=int, default=None,
                        help="Repeticiones por tamaño")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-warmup", action="store_true",
                        help="Saltar el warm-up de OSRM")
    parser.add_argument("--output", type=str, default=None,
                        help="Path al JSON de salida (default: results/osrm_benchmark_<ts>.json)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.sizes:
        sizes = [int(s) for s in args.sizes.split(",")]
    elif args.quick:
        sizes = QUICK_SIZES
    else:
        sizes = DEFAULT_SIZES

    if args.reps is not None:
        repetitions = args.reps
    elif args.quick:
        repetitions = QUICK_REPETITIONS
    else:
        repetitions = DEFAULT_REPETITIONS

    logger.info("OSRM base_url=%s sizes=%s reps=%d", args.base_url, sizes, repetitions)

    results = run_benchmark(
        base_url=args.base_url,
        sizes=sizes,
        repetitions=repetitions,
        seed=args.seed,
        warmup=not args.no_warmup,
    )

    print_table(results)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output = (
        Path(args.output) if args.output
        else Path(__file__).resolve().parent / "results" / f"osrm_benchmark_{timestamp}.json"
    )
    save_json(
        results,
        output,
        meta={
            "base_url": args.base_url,
            "timestamp": timestamp,
            "sizes": sizes,
            "repetitions": repetitions,
            "seed": args.seed,
            "warmup": not args.no_warmup,
        },
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
