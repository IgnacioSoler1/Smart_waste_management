# Power Budget — Cálculo de consumo y duración de batería

## Consumo por estado

| Estado | Corriente | Duración | Energía (mAh) |
|--------|-----------|----------|---------------|
| Deep sleep (ESP32 + SIM800L off) | ~10 μA | 875 s | 0.0024 |
| Wake + init sensores | ~80 mA | 0.5 s | 0.011 |
| Medición (5 lecturas × 2 sensores) | ~100 mA | 1 s | 0.028 |
| SIM800L power on + boot | ~50 mA | 3 s | 0.042 |
| GPRS connect (registración + PDP) | ~200 mA | 10 s | 0.556 |
| NTP sync | ~150 mA | 1 s | 0.042 |
| TLS handshake | ~150 mA | 4 s | 0.167 |
| MQTT publish | ~200 mA | 0.5 s | 0.028 |
| Disconnect + power off modem | ~100 mA | 2 s | 0.056 |
| **Pico de transmisión SIM800L** | **~2000 mA** | **pulsos de 577μs** | — |

## Consumo por ciclo (15 min)

| Fase | Energía |
|------|---------|
| Activo (~22 s) | ~0.93 mAh |
| Sleep (~878 s) | ~0.0024 mAh |
| **Total por ciclo** | **~0.93 mAh** |

## Consumo diario

- Ciclos por día: 24 × 4 = 96
- Consumo diario: 96 × 0.93 = **~89 mAh/día**

## Duración de batería

| Batería | Capacidad | Duración estimada |
|---------|-----------|-------------------|
| 1× 18650 (3.7V) | 3000 mAh | ~33 días |
| 2× 18650 (paralelo) | 6000 mAh | ~67 días |
| 3× 18650 (paralelo) | 9000 mAh | ~101 días |
| 4× 18650 (paralelo) | 12000 mAh | ~135 días |
| Panel solar (5V, 1W) + 2× 18650 | ∞ (teórico) | >1 año (con sol en Montevideo) |

## Optimizaciones posibles

1. **Publicar solo si hay cambio significativo** (>5% de cambio en fill_level): reduce ciclos GPRS de 96/día a ~20/día en contenedores de llenado lento.

2. **Aumentar intervalo de sleep** en horarios de baja actividad (noche): 30 min en vez de 15 min entre 23:00-06:00.

3. **Usar PSM (Power Saving Mode) de la red 2G** si el operador lo soporta.

4. **Eliminar el VL53L1X** si el JSN-SR04T es suficientemente confiable: ahorra ~30mA durante medición.

## Notas

- Los valores de corriente son estimaciones basadas en datasheets. Medir con multímetro en el prototipo.
- El pico de 2A del SIM800L durante TX requiere un capacitor grande (1000μF) en la alimentación.
- La eficiencia del regulador DC-DC afecta el consumo real (asumir ~85% de eficiencia).
- La temperatura afecta la capacidad de las baterías LiPo (reducción del ~20% a 0°C).
  Montevideo raramente baja de 5°C, así que el impacto es menor.
