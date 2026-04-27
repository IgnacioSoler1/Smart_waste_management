"""
fill_model.py — SmartWaste MVD

Modelo de simulación del llenado de contenedores de residuos domiciliarios.

La curva de llenado es exponencial-saturante (a veces llamada "logística
simplificada" en literatura de waste management):

    fill = 100 * (1 - exp(-rate * hours_since_empty / 100))

donde rate es el producto de factores que capturan:
  - Variación horaria del día (picos en mañana, mediodía y noche)
  - Variación por día de la semana (lunes-viernes > fin de semana)
  - Densidad de la zona (centro vs periferia)

Uso:
  from simulator.fill_model import FillModel
  model = FillModel()
  level = model.calculate_fill_level(container_info, current_time, last_emptied)
"""

import math
import random
from datetime import datetime, timezone
from typing import Any

from simulator.zone_density import get_zone_factor

# ─────────────────────────────────────────────────────────
# Precómputo de la suma acumulada de factores horarios
#
# Se usa en _avg_hourly_factor() para calcular el factor horario
# promedio sobre un período arbitrario en O(1), sin iterar hora a hora.
# HOURLY_CUMSUM[h] = suma de HOURLY_FACTORS[0..h-1] (exclusivo)
# ─────────────────────────────────────────────────────────

# (se inicializa después de definir HOURLY_FACTORS, más abajo)
_HOURLY_CUMSUM: list[float] = []
_DAILY_FACTOR_SUM: float = 0.0

# ─────────────────────────────────────────────────────────
# Parámetros del modelo
# ─────────────────────────────────────────────────────────

# Tasa base de llenado en %/hora, representando el promedio
# para un contenedor residencial estándar de 2400 L.
BASE_RATE: float = 2.0

# Desviación estándar del ruido gaussiano agregado al nivel final.
# Modela variabilidad entre contenedores del mismo circuito: días de
# mercado, eventos, densidad local, etc.
NOISE_STD: float = 2.0

# Factor horario: cómo varía la tasa de generación de residuos a lo
# largo del día. Calibrado para Montevideo:
#   - Valle nocturno (2-6am): 0.3  — mínima actividad residencial
#   - Pico matinal  (8-10am): 1.5  — desayuno, salida al trabajo
#   - Pico mediodía (12-14h): 1.3  — almuerzos, comercio
#   - Pico vespertino (18-21): 1.8 — cena, mayor pico del día
HOURLY_FACTORS: dict[int, float] = {
    0:  0.4,   # medianoche
    1:  0.4,
    2:  0.3,   # valle nocturno ──────────────────────────
    3:  0.3,
    4:  0.3,
    5:  0.3,   # ──────────────────────────────────────────
    6:  0.8,   # amanecer, primeros movimientos
    7:  0.9,
    8:  1.5,   # pico matinal ────────────────────────────
    9:  1.5,
    10: 1.5,   # ──────────────────────────────────────────
    11: 1.1,
    12: 1.3,   # pico mediodía ───────────────────────────
    13: 1.3,
    14: 1.3,   # ──────────────────────────────────────────
    15: 1.0,
    16: 1.0,
    17: 1.0,
    18: 1.8,   # pico vespertino ─────────────────────────
    19: 1.8,
    20: 1.8,
    21: 1.8,   # ──────────────────────────────────────────
    22: 1.1,
    23: 0.7,
}

# Factor por día de la semana. Los fines de semana hay menos actividad
# comercial pero más cocina en casa → baja moderada, no total.
# weekday() → 0=lunes, 6=domingo
DAY_FACTORS: dict[int, float] = {
    0: 1.0,   # lunes
    1: 1.0,   # martes
    2: 1.0,   # miércoles
    3: 1.0,   # jueves
    4: 1.0,   # viernes
    5: 0.8,   # sábado
    6: 0.6,   # domingo
}

# Rango permitido del zone_factor (validación de entrada)
ZONE_FACTOR_MIN: float = 0.5
ZONE_FACTOR_MAX: float = 3.0

# Inicializar tabla de suma acumulada ahora que HOURLY_FACTORS está definido.
# _HOURLY_CUMSUM[h] = suma de HOURLY_FACTORS[0] + ... + HOURLY_FACTORS[h-1]
_HOURLY_CUMSUM = [0.0] * 25
for _h in range(24):
    _HOURLY_CUMSUM[_h + 1] = _HOURLY_CUMSUM[_h] + HOURLY_FACTORS[_h]
_DAILY_FACTOR_SUM = _HOURLY_CUMSUM[24]   # ≈ 25.2


def _avg_hourly_factor(start_hour: int, total_hours: float) -> float:
    """
    Calcula el factor horario promedio sobre un período de `total_hours`
    horas comenzando a las `start_hour` (0–23).

    Usa la tabla de suma acumulada para calcular en O(1) la suma de los
    factores sobre cualquier número de horas, sin iterar.

    Ejemplo:
      _avg_hourly_factor(6, 12)  → promedio de factores entre 06:00 y 18:00
                                   = (0.8 + 0.9 + 1.5*3 + 1.1 + 1.3*3) / 12
                                   ≈ 1.21

    Por qué importa la monotonía:
      Si se usara el factor instantáneo de `current_time`, un contenedor
      calculado a medianoche tendría una tasa baja (0.4) y un fill menor
      que el calculado al mediodía — físicamente imposible. Promediar
      sobre todo el período de llenado garantiza que fill sea monotónica
      en horas_transcurridas.
    """
    if total_hours <= 0.0:
        return HOURLY_FACTORS[start_hour % 24]

    # Horas completas y fracción restante
    int_hours = int(total_hours)
    frac = total_hours - int_hours

    # Suma de los factores por los int_hours completos empezando en start_hour
    # Se calcula sumando días completos + el tramo parcial dentro del primer día
    n_full_days = int_hours // 24
    remainder_hours = int_hours % 24

    total_factor = n_full_days * _DAILY_FACTOR_SUM

    # Tramo parcial: horas start_hour, start_hour+1, ... start_hour+remainder-1 (mod 24)
    sh = start_hour % 24
    end_h = (sh + remainder_hours) % 24

    if sh + remainder_hours <= 24:
        # Sin vuelta de medianoche
        total_factor += _HOURLY_CUMSUM[sh + remainder_hours] - _HOURLY_CUMSUM[sh]
    else:
        # Con vuelta de medianoche
        total_factor += (_DAILY_FACTOR_SUM - _HOURLY_CUMSUM[sh]) + _HOURLY_CUMSUM[end_h]

    # Fracción de la hora siguiente (interpolación lineal)
    next_h = (sh + remainder_hours) % 24
    total_factor += frac * HOURLY_FACTORS[next_h]

    return total_factor / total_hours


# ─────────────────────────────────────────────────────────
# Clase principal
# ─────────────────────────────────────────────────────────

class FillModel:
    """
    Modelo de llenado de contenedores de residuos.

    Instanciar una vez y reutilizar para múltiples contenedores.
    Los parámetros base son configurables en el constructor para
    facilitar los tests y el tuning del modelo.

    Args:
        base_rate:  tasa base de llenado (%/hora). Default 2.0.
        noise_std:  desviación estándar del ruido gaussiano. Default 2.0.
        seed:       semilla del generador aleatorio. None = aleatorio.
    """

    def __init__(
        self,
        base_rate: float = BASE_RATE,
        noise_std: float = NOISE_STD,
        seed: int | None = None,
    ) -> None:
        self.base_rate = base_rate
        self.noise_std = noise_std
        self._rng = random.Random(seed)

    # ── Factores ──────────────────────────────────────────

    def hour_factor(self, dt: datetime) -> float:
        """Factor multiplicativo según la hora del día (0-23)."""
        return HOURLY_FACTORS[dt.hour]

    def day_factor(self, dt: datetime) -> float:
        """Factor multiplicativo según el día de la semana (0=lunes)."""
        return DAY_FACTORS[dt.weekday()]

    def _resolve_zone_factor(self, container_info: dict[str, Any]) -> float:
        """
        Obtiene el zone_factor desde container_info.

        Prioridad:
          1. Campo 'zone_factor' explícito en el dict → úsalo directamente.
          2. Campos 'latitude' + 'longitude' → calcula con get_zone_factor().
          3. Fallback → 1.0 (zona sin información).
        """
        if "zone_factor" in container_info:
            zf = float(container_info["zone_factor"])
            return max(ZONE_FACTOR_MIN, min(ZONE_FACTOR_MAX, zf))

        lat = container_info.get("latitude")
        lon = container_info.get("longitude")
        if lat is not None and lon is not None:
            return get_zone_factor(float(lat), float(lon))

        return 1.0

    def effective_rate(
        self,
        zone_factor: float,
        current_time: datetime,
    ) -> float:
        """
        Tasa instantánea de llenado en el momento actual.

            rate = base_rate × hour_factor(now) × day_factor(now) × zone_factor

        Usar para estimar cuánto llenará el contenedor en la PRÓXIMA hora.
        No usar directamente en la curva histórica (ver calculate_fill_level).

        Args:
            zone_factor:   factor de densidad de la zona (0.5 – 3.0)
            current_time:  momento de la consulta

        Returns:
            Tasa instantánea en %/hora (≥ 0).
        """
        return (
            self.base_rate
            * self.hour_factor(current_time)
            * self.day_factor(current_time)
            * zone_factor
        )

    def _accumulated_rate(
        self,
        zone_factor: float,
        last_emptied_time: datetime,
        current_time: datetime,
        hours_since_empty: float,
    ) -> float:
        """
        Tasa promedio de llenado sobre todo el período desde el último vaciado.

            avg_rate = base_rate × avg_hour_factor × day_factor(now) × zone_factor

        Usar avg_hour_factor (integrado sobre el período) en vez del factor
        instantáneo garantiza que fill sea monotónica: a más horas transcurridas,
        mayor fill, independientemente de si la consulta ocurre a medianoche
        o a mediodía.

        El day_factor se toma del día actual (simplificación aceptable para
        períodos de hasta ~7 días; para períodos más largos la diferencia
        lunes/domingo se promediaría sola de todas formas).
        """
        avg_hf = _avg_hourly_factor(last_emptied_time.hour, hours_since_empty)
        return (
            self.base_rate
            * avg_hf
            * self.day_factor(current_time)
            * zone_factor
        )

    # ── Curva de llenado ──────────────────────────────────

    def calculate_fill_level(
        self,
        container_info: dict[str, Any],
        current_time: datetime,
        last_emptied_time: datetime | None,
    ) -> float:
        """
        Calcula el nivel de llenado estimado del contenedor (0–100).

        Modelo:
            fill = 100 × (1 − exp(−rate × hours_since_empty / 100))

        La curva satura en 100 de forma asintótica. Un contenedor de
        zona centro (zone_factor=2.5) con base_rate=2.0 alcanza ~87%
        a las 40 horas; uno suburbano (0.7) necesita ~143 horas para
        el mismo nivel.

        Args:
            container_info:   dict con campos del contenedor (DynamoDB item
                              o containers_enriched.json). Necesita al menos
                              'latitude'+'longitude' o 'zone_factor'.
            current_time:     momento de la consulta (UTC aware o naive).
            last_emptied_time: última vez que se vació el contenedor.
                              None → se asume que empezó a llenarse hace
                              48 horas (estado inicial desconocido).

        Returns:
            Nivel de llenado entre 0.0 y 100.0 (incluyendo ruido gaussiano
            con std=2.0, truncado al rango [0, 100]).
        """
        # ── Horas transcurridas ───────────────────────────
        if last_emptied_time is None:
            # Sin historial: asumir 48 h para no arrancar desde 0.
            # Se usa un tiempo de referencia ficticio para _accumulated_rate.
            hours_since_empty = 48.0
            _ref_emptied = current_time
        else:
            delta = current_time - last_emptied_time
            hours_since_empty = max(0.0, delta.total_seconds() / 3600.0)
            _ref_emptied = last_emptied_time

        zone_factor = self._resolve_zone_factor(container_info)

        # ── Tasa promedio sobre el período completo ───────
        # Usar el factor horario PROMEDIO sobre [last_emptied, current_time]
        # en vez del instantáneo garantiza monotonía: a más horas, más fill,
        # independientemente de si se consulta a medianoche o al mediodía.
        rate = self._accumulated_rate(
            zone_factor, _ref_emptied, current_time, hours_since_empty
        )

        # ── Curva exponencial-saturante ───────────────────
        fill = 100.0 * (1.0 - math.exp(-rate * hours_since_empty / 100.0))

        # Ruido gaussiano: modela variabilidad real entre contenedores
        noise = self._rng.gauss(0.0, self.noise_std)

        # Clamp al rango físico [0, 100]
        return max(0.0, min(100.0, fill + noise))

    # ── Utilidades de diagnóstico ─────────────────────────

    def fill_curve_hours_to_pct(
        self,
        container_info: dict[str, Any],
        target_pct: float,
        reference_time: datetime,
    ) -> float:
        """
        Calcula cuántas horas tarda el contenedor en alcanzar target_pct.

        Inversa analítica de la curva (sin ruido), útil para planificación
        de rutas y para el dashboard.

            t = -100 × ln(1 - target_pct/100) / rate

        Args:
            container_info:  dict del contenedor
            target_pct:      nivel objetivo (0 < target_pct < 100)
            reference_time:  hora de referencia para calcular rate

        Returns:
            Horas hasta alcanzar target_pct. Retorna inf si rate ≤ 0.
        """
        if target_pct <= 0:
            return 0.0
        if target_pct >= 100:
            return math.inf

        zone_factor = self._resolve_zone_factor(container_info)
        rate = self.effective_rate(zone_factor, reference_time)

        if rate <= 0:
            return math.inf

        return -100.0 * math.log(1.0 - target_pct / 100.0) / rate


# ─────────────────────────────────────────────────────────
# Demo: simulación de 72 horas en un contenedor del centro
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import timedelta

    # Contenedor representativo del Municipio B (Centro, Cordón)
    # Coordenadas aprox. de la Plaza Independencia, Ciudad Vieja
    DEMO_CONTAINER: dict[str, Any] = {
        "container_id": "DEMO-001",
        "circuit_id":   "B_DU_RM_CL_001",
        "latitude":     -34.9058,
        "longitude":    -56.1913,
        "capacity_liters": 2400,
    }

    # Semilla fija para reproducibilidad del demo
    model = FillModel(seed=42)

    # Empezamos un lunes a las 06:00 UTC
    start = datetime(2024, 1, 1, 6, 0, 0, tzinfo=timezone.utc)
    last_emptied = start  # recién vaciado

    zone_factor = get_zone_factor(
        DEMO_CONTAINER["latitude"], DEMO_CONTAINER["longitude"]
    )
    hours_to_80 = model.fill_curve_hours_to_pct(
        DEMO_CONTAINER, 80.0, start
    )

    # ── Header ────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  SmartWaste MVD — Simulación de llenado (72 h)")
    print("=" * 60)
    print(f"  Contenedor : {DEMO_CONTAINER['container_id']}")
    print(f"  Ubicación  : ({DEMO_CONTAINER['latitude']}, {DEMO_CONTAINER['longitude']})")
    print(f"  Zone factor: {zone_factor:.1f}  (Municipio B — Centro)")
    print(f"  Base rate  : {model.base_rate:.1f} %/h")
    print(f"  Vaciado el : {last_emptied.strftime('%a %d/%m %H:%M')} UTC")
    print(f"  Est. horas hasta 80%: {hours_to_80:.1f} h")
    print()
    print(f"  {'Tiempo':>18}  {'Horas':>5}  {'Fill':>6}  {'Barra'}")
    print("  " + "─" * 56)

    BAR_WIDTH = 30

    for step in range(13):  # 0, 6, 12, ..., 72 horas
        offset_hours = step * 6
        current = start + timedelta(hours=offset_hours)

        fill = model.calculate_fill_level(DEMO_CONTAINER, current, last_emptied)

        # Barra visual
        filled_chars = round(fill / 100 * BAR_WIDTH)
        bar = "█" * filled_chars + "░" * (BAR_WIDTH - filled_chars)

        # Indicador de urgencia
        urgency = "🔴 FULL" if fill >= 90 else "🟡 HIGH" if fill >= 70 else "🟢     "

        print(
            f"  {current.strftime('%a %d/%m %H:%M UTC'):>18}"
            f"  {offset_hours:>5}h"
            f"  {fill:>5.1f}%"
            f"  [{bar}]  {urgency}"
        )

    print()
    print("  Nota: los valores incluyen ruido gaussiano (std=2.0).")
    print("        Correr de nuevo para ver variación.")
    print("=" * 60)
    print()
