# Hardware Setup — ESP32 + SIM800L + Sensores

## Componentes

| Componente | Modelo | Cantidad | Función |
|-----------|--------|----------|---------|
| MCU | ESP32-WROOM-32 | 1 | Controlador principal |
| Modem | SIM800L (mini) | 1 | Conectividad GPRS/2G |
| Ultrasonido | JSN-SR04T | 1 | Sensor de distancia (waterproof) |
| ToF Laser | VL53L1X | 1 | Sensor de distancia (backup/validación) |
| Regulador | LM2596 o similar | 1 | 12V/5V → 3.3V para ESP32, 4.0V para SIM800L |
| Batería | 18650 LiPo 3.7V (x3 o x4) | 1 pack | Alimentación |
| Antena | GSM 900/1800 MHz | 1 | Para SIM800L |
| SIM | Antel (Uruguay) | 1 | Datos 2G/GPRS |

## Conexiones (pines por defecto)

```
ESP32                   SIM800L
─────                   ───────
GPIO4  (TX) ──────────▶ RXD
GPIO5  (RX) ◀────────── TXD
GPIO23       ──────────▶ PWRKEY (via NPN transistor)
GND          ──────────▶ GND
             ◀────────── VCC (4.0V separado, no compartir regulador con ESP32)

ESP32                   JSN-SR04T (UART mode)
─────                   ─────────
GPIO17 (TX) ──────────▶ RX (trigger)
GPIO16 (RX) ◀────────── TX (echo data)
3.3V        ──────────▶ VCC (o 5V con level shifter)
GND         ──────────▶ GND

ESP32                   VL53L1X
─────                   ───────
GPIO21 (SDA) ─────────▶ SDA (con pull-up 4.7kΩ a 3.3V)
GPIO22 (SCL) ─────────▶ SCL (con pull-up 4.7kΩ a 3.3V)
3.3V         ─────────▶ VCC
GND          ─────────▶ GND

ESP32                   Batería
─────                   ───────
GPIO34 (ADC) ◀───┐
                  ├── Divisor de voltaje (100kΩ + 100kΩ)
VBAT ────────────┘
```

## Notas importantes

### SIM800L
- **Alimentación**: El SIM800L necesita 3.4V-4.4V y puede consumir picos de 2A durante transmisión.
  Usar un regulador dedicado (LM2596) con capacitor de 1000μF en la salida.
  NO alimentar desde el regulador 3.3V del ESP32.
- **PWRKEY**: Controlar via un transistor NPN (2N2222), no directamente desde el GPIO.
- **Antena**: Usar una antena GSM externa con conector u.FL o SMA.

### JSN-SR04T
- **Modo UART**: Asegurar que el jumper del JSN-SR04T esté en modo UART (no trigger/echo).
  En modo UART, el baudrate es 9600 fijo.
- **Waterproof**: La sonda es IP67, ideal para contenedores expuestos a lluvia.
- **Rango**: 250mm - 4500mm. Para contenedores más bajos que 250mm, solo usar VL53L1X.

### VL53L1X
- **I2C pull-ups**: Necesarios si el breakout board no los incluye. 4.7kΩ a 3.3V.
- **No es waterproof**: Montar dentro del contenedor, protegido de lluvia directa.
- **Rango**: Hasta 4000mm en long distance mode, pero la precisión baja después de 2000mm.

### Montaje en contenedor
- Los sensores van montados en la tapa del contenedor, apuntando hacia abajo.
- El enclosure debe ser IP65 mínimo (lluvia y polvo).
- La antena GSM debe quedar fuera del enclosure metálico.
- Considerar un panel solar pequeño (5V, 1W) para extender la vida de la batería.
