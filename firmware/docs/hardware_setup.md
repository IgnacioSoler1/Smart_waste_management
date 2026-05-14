# Hardware Setup — ESP32 + SIM800L + Sensors

## Components

| Component | Model | Qty | Function |
|-----------|-------|-----|----------|
| MCU | ESP32-WROOM-32 | 1 | Main controller |
| Modem | SIM800L (mini) | 1 | GPRS/2G connectivity |
| Ultrasonic | JSN-SR04T | 1 | Distance sensor (waterproof) |
| ToF Laser | VL53L1X | 1 | Distance sensor (backup/validation) |
| Regulator | LM2596 or similar | 1 | 12V/5V → 3.3V for ESP32, 4.0V for SIM800L |
| Battery | 18650 LiPo 3.7V (x3 or x4) | 1 pack | Power supply |
| Antenna | GSM 900/1800 MHz | 1 | For SIM800L |
| SIM card | Antel (Uruguay) | 1 | 2G/GPRS data |

## Wiring (default pins)

```
ESP32                   SIM800L
─────                   ───────
GPIO4  (TX) ──────────▶ RXD
GPIO5  (RX) ◀────────── TXD
GPIO23       ──────────▶ PWRKEY (via NPN transistor)
GND          ──────────▶ GND
             ◀────────── VCC (separate 4.0V supply, do NOT share regulator with ESP32)

ESP32                   JSN-SR04T (UART mode)
─────                   ─────────
GPIO17 (TX) ──────────▶ RX (trigger)
GPIO16 (RX) ◀────────── TX (echo data)
3.3V        ──────────▶ VCC (or 5V with level shifter)
GND         ──────────▶ GND

ESP32                   VL53L1X
─────                   ───────
GPIO21 (SDA) ─────────▶ SDA (with 4.7kΩ pull-up to 3.3V)
GPIO22 (SCL) ─────────▶ SCL (with 4.7kΩ pull-up to 3.3V)
3.3V         ─────────▶ VCC
GND          ─────────▶ GND

ESP32                   Battery
─────                   ───────
GPIO34 (ADC) ◀───┐
                  ├── Voltage divider (100kΩ + 100kΩ)
VBAT ────────────┘
```

## Important notes

### SIM800L
- **Power supply**: The SIM800L requires 3.4V–4.4V and can draw up to 2A peak during transmission.
  Use a dedicated regulator (LM2596) with a 1000μF capacitor on the output.
  Do NOT power it from the ESP32's 3.3V regulator.
- **PWRKEY**: Drive it through an NPN transistor (2N2222), not directly from the GPIO.
- **Antenna**: Use an external GSM antenna with u.FL or SMA connector.

### JSN-SR04T
- **UART mode**: Make sure the JSN-SR04T jumper is set to UART mode (not trigger/echo).
  In UART mode, the baud rate is fixed at 9600.
- **Waterproof**: The probe is IP67, ideal for containers exposed to rain.
- **Range**: 250mm – 4500mm. For containers shorter than 250mm, use only the VL53L1X.

### VL53L1X
- **I2C pull-ups**: Required if the breakout board doesn't include them. Use 4.7kΩ to 3.3V.
- **Not waterproof**: Mount inside the container, protected from direct rain.
- **Range**: Up to 4000mm in long distance mode, but accuracy degrades beyond 2000mm.

### Container mounting
- Sensors are mounted on the container lid, pointing downward.
- The enclosure must be at least IP65 rated (rain and dust protection).
- The GSM antenna must be placed outside the metal enclosure.
- Consider a small solar panel (5V, 1W) to extend battery life.
