"""
constraints.py — SmartWaste MVD

Funciones de dominio para traducir el estado de los contenedores
a las restricciones del problema VRP (Vehicle Routing Problem).
"""


def estimate_demand_kg(
    fill_level: float,
    capacity_liters: float = 2400,
) -> float:
    """
    Estima el peso de residuos en kg para un contenedor dado su nivel de llenado.

    Fórmula: fill_level/100 * capacity_liters * density_kg_per_liter

    Args:
        fill_level:      Nivel de llenado en porcentaje [0, 100].
        capacity_liters: Capacidad volumétrica del contenedor en litros.
                         Default 2400 L (contenedor estándar de Montevideo).

    Returns:
        Peso estimado en kg.

    Densidad de referencia:
        Residuos domésticos mixtos: 0.30 kg/L (rango típico: 0.20–0.45 kg/L).
        Un contenedor de 2400 L al 100% pesa ~720 kg.
        Un camión de 25 t puede vaciar ~34 contenedores llenos en un turno.
    """
    density_kg_per_liter = 0.30
    return (fill_level / 100.0) * capacity_liters * density_kg_per_liter


def calculate_prize(fill_level: float) -> float:
    """
    Calcula el "premio" de visitar un contenedor según su nivel de llenado.

    El prize controla la lógica de paradas opcionales en el VRP solver:
    - Prize muy alto → el solver nunca lo saltea (obligatorio)
    - Prize medio → lo visita si no implica un desvío grande
    - Prize bajo → solo lo visita si queda de paso

    El valor del prize se compara contra el costo de desvío en segundos
    (la cost_matrix del OSRM está en segundos). Un prize de 600 significa
    que el solver acepta un desvío de hasta ~10 minutos para visitarlo.

    Args:
        fill_level: Nivel de llenado [0, 100].

    Returns:
        Valor del prize (float). 0.0 si el contenedor debe excluirse.

    Umbrales:
        > 95%:  10000 — prácticamente obligatorio (overflow inminente)
        > 60%:  3000  — alta prioridad, el solver casi siempre lo incluye
        > 35%:  valor proporcional al fill_level — lo visita si queda de paso
        ≤ 20%:  0     — excluir del problema (no vale la pena ni de paso)
    """
    if fill_level > 90:
        return 10_000.0
    if fill_level > 60:
        return 3_000.0
    if fill_level > 40:
        # Escala lineal entre 200 (al 21%) y 1500 (al 60%)
        # Esto genera prizes que justifican desvíos de ~3-25 minutos
        return 200.0 + (fill_level - 20.0) * (1300.0 / 40.0)
    return 0.0


def get_time_window(shift: str) -> tuple[int, int]:
    """
    Devuelve la ventana de tiempo operativa para un turno de recolección,
    expresada en segundos desde la medianoche del día actual.

    Args:
        shift: Identificador del turno. Valores aceptados (case-insensitive):
               "morning" / "matutino"   → turno mañana
               "afternoon" / "vespertino" → turno tarde
               "night" / "nocturno"     → turno noche (cruza medianoche)

    Returns:
        (earliest_second, latest_second) desde medianoche.
        El turno nocturno usa latest=30*3600 (30 h = 6 h del día siguiente)
        para representar el cruce de medianoche sin necesitar aritmética
        modular en el solver.

    Raises:
        ValueError: Si el valor de shift no es reconocido.

    Turnos de Montevideo (Intendencia):
        Mañana:  06:00 – 14:00
        Tarde:   14:00 – 22:00
        Noche:   22:00 – 06:00 (+1 día)
    """
    normalized = shift.lower().strip()

    if normalized in ("morning", "matutino", "m"):
        return (6 * 3600, 14 * 3600)        # 06:00 – 14:00

    if normalized in ("afternoon", "vespertino", "v"):
        return (14 * 3600, 22 * 3600)       # 14:00 – 22:00

    if normalized in ("night", "nocturno", "n"):
        return (22 * 3600, 30 * 3600)       # 22:00 – 06:00 (+1d, en horizonte 30h)

    raise ValueError(
        f"Turno desconocido: {shift!r}. "
        f"Valores válidos: 'morning', 'afternoon', 'night' "
        f"(también acepta español: 'matutino', 'vespertino', 'nocturno')."
    )
