# Power Budget — Energy consumption and battery life estimation

## Current draw per state

| State | Current | Duration | Energy (mAh) |
|-------|---------|----------|--------------|
| Deep sleep (ESP32 + SIM800L off) | ~10 μA | variable | ~0.003/min |
| Wake + sensor init | ~80 mA | 0.5 s | 0.011 |
| Measurement (5 readings × 2 sensors) | ~100 mA | 1 s | 0.028 |
| SIM800L power on + boot | ~50 mA | 3 s | 0.042 |
| GPRS connect (registration + PDP) | ~200 mA | 10 s | 0.556 |
| NTP sync | ~150 mA | 1 s | 0.042 |
| TLS handshake | ~150 mA | 4 s | 0.167 |
| MQTT publish | ~200 mA | 0.5 s | 0.028 |
| Disconnect + power off modem | ~100 mA | 2 s | 0.056 |
| **SIM800L TX burst** | **~2000 mA** | **577μs pulses** | — |

## Energy per cycle

The active phase (~22 seconds) consumes approximately **0.93 mAh** regardless of the sleep interval.

## Comparison by measurement interval

| Interval | Cycles/day | Active energy (mAh/day) | Sleep energy (mAh/day) | **Total (mAh/day)** |
|----------|-----------|------------------------|----------------------|-------------------|
| **15 min** | 96 | 89.3 | 0.23 | **~89.5** |
| **20 min** | 72 | 67.0 | 0.23 | **~67.2** |
| **30 min** | 48 | 44.6 | 0.23 | **~44.9** |

> Sleep energy is nearly constant (~0.23 mAh/day at 10μA) since the device sleeps ~99.97% of the time regardless of interval.

## Battery life by interval

### 15-minute interval (~89.5 mAh/day)

| Battery | Capacity | Estimated life |
|---------|----------|---------------|
| 1× 18650 (3.7V) | 3000 mAh | ~33 days |
| 2× 18650 (parallel) | 6000 mAh | ~67 days |
| 3× 18650 (parallel) | 9000 mAh | ~100 days |
| 4× 18650 (parallel) | 12000 mAh | ~134 days |

### 20-minute interval (~67.2 mAh/day)

| Battery | Capacity | Estimated life |
|---------|----------|---------------|
| 1× 18650 (3.7V) | 3000 mAh | ~45 days |
| 2× 18650 (parallel) | 6000 mAh | ~89 days |
| 3× 18650 (parallel) | 9000 mAh | ~134 days |
| 4× 18650 (parallel) | 12000 mAh | ~179 days |

### 30-minute interval (~44.9 mAh/day)

| Battery | Capacity | Estimated life |
|---------|----------|---------------|
| 1× 18650 (3.7V) | 3000 mAh | ~67 days |
| 2× 18650 (parallel) | 6000 mAh | ~134 days |
| 3× 18650 (parallel) | 9000 mAh | ~200 days |
| 4× 18650 (parallel) | 12000 mAh | ~267 days |

### With solar panel

| Config | Battery life |
|--------|-------------|
| Solar panel (5V, 1W) + 2× 18650 | >1 year (theoretical, depends on sunlight in Montevideo) |

## Possible optimizations

1. **Publish only on significant change** (>5% fill_level delta): reduces GPRS cycles from 96/day to ~20/day for slow-filling containers.

2. **Increase sleep interval during low-activity hours** (night): 30 min instead of 15 min between 23:00–06:00.

3. **Use PSM (Power Saving Mode)** on the 2G network if the carrier supports it.

4. **Remove the VL53L1X** if the JSN-SR04T alone is reliable enough: saves ~30mA during measurement.

## Notes

- Current values are estimates based on datasheets. Measure with a multimeter on the actual prototype.
- The 2A SIM800L TX peak requires a large capacitor (1000μF) on the power supply.
- DC-DC regulator efficiency affects real-world consumption (assume ~85% efficiency).
- Temperature affects LiPo battery capacity (~20% reduction at 0°C).
  Montevideo rarely drops below 5°C, so the impact is minimal.
