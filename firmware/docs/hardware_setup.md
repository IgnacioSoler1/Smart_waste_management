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

---

## Dev mode (sin hardware — solo ESP32 + WiFi)

Si queres probar el pipeline completo (ESP32 → IoT Core → Lambda → DynamoDB) sin sensores ni SIM800L, podes usar el modo dev. Solo necesitas la ESP32 conectada a tu computadora y una red WiFi.

### Quick start modo dev

```bash
# 1. Activar entorno ESP-IDF
source "$HOME/.espressif/tools/activate_idf_v6.0.1.sh"
cd firmware

# 2. Configurar target (solo la primera vez)
idf.py set-target esp32

# 3. Compilar con defaults de desarrollo
#    Esto habilita WiFi + sensores simulados + sleep de 60s
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.dev" build

# 4. Configurar WiFi y endpoint MQTT
idf.py menuconfig
#    → SmartWaste Sensor Configuration → Connectivity Mode → WiFi SSID/Password
#    → SmartWaste Sensor Configuration → MQTT → Broker URI

# 5. Flashear certificados NVS (mismos pasos que producción — ver paso 6 abajo)

# 6. Flashear y monitorear
idf.py -p /dev/cu.usbserial-0001 flash monitor
```

En modo dev el firmware:
- Se conecta por **WiFi** en vez de GPRS (no necesita SIM800L)
- Genera datos de **fill_level simulados** (random 0-100%) en vez de leer sensores
- Duerme solo **60 segundos** entre ciclos (en vez de 15 min)
- Publica al mismo topic MQTT que producción → el pipeline completo funciona igual

---

## Flashing guide — de cero a sensor funcionando

Esta guia explica paso a paso como preparar, configurar y cargar el firmware en una ESP32 para que funcione como sensor de llenado SmartWaste. Incluye la configuracion de certificados para que el dispositivo se conecte automaticamente a AWS IoT Core.

### Requisitos previos

Antes de empezar necesitas tener instalado:

| Herramienta | Version | Para que se usa |
|-------------|---------|-----------------|
| **ESP-IDF** | v5.1 o superior | Framework de desarrollo para ESP32 (compilar, flashear, monitor) |
| **Python** | 3.8+ | Herramientas de ESP-IDF y generador de particiones NVS |
| **AWS CLI** | v2 | Registrar la CA y verificar dispositivos en IoT Core |
| **openssl** | cualquiera | Generar certificados TLS |

Si no tenes ESP-IDF instalado, segui la [guia oficial de Espressif](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/).

### Paso 1 — Preparar el entorno ESP-IDF

Cada vez que abras una terminal nueva para trabajar con el firmware, necesitas cargar el entorno de ESP-IDF:

```bash
# Si instalaste con eim (Espressif Installation Manager):
source "$HOME/.espressif/tools/activate_idf_v6.0.1.sh"

# Si instalaste manualmente (git clone):
# . $HOME/esp/esp-idf/export.sh

# Navegar al directorio del firmware
cd firmware

# Configurar el target (solo la primera vez)
idf.py set-target esp32
```

> **Tip:** Para no tener que escribir el source cada vez, podes agregar un alias en tu `~/.zshrc`:
> ```bash
> alias idf='source "$HOME/.espressif/tools/activate_idf_v6.0.1.sh"'
> ```
> Despues solo ejecutas `idf` para activar el entorno.

### Paso 2 — Descargar el certificado raiz de Amazon (Root CA)

El firmware necesita el certificado raiz de Amazon para validar la conexion TLS con AWS IoT Core. Este certificado es publico (no es un secreto) y es el mismo para todos los dispositivos:

```bash
curl -o certs/AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
```

Este archivo se embebe en el binario del firmware durante la compilacion (definido en `main/CMakeLists.txt` via `EMBED_TXTFILES`). No necesitas copiarlo a ningun otro lugar.

### Paso 3 — Registrar la CA en AWS IoT Core (una sola vez)

Este paso se hace **una unica vez** y sirve para habilitar JITR (Just-in-Time Registration). JITR permite que cada ESP32 nueva se registre automaticamente en AWS IoT Core la primera vez que se conecta, sin tener que crear Things manualmente en la consola.

**Como funciona JITR:**
1. Creas una CA (Certificate Authority) propia y la registras en AWS IoT Core
2. A cada ESP32 le generas un certificado firmado por esa CA
3. Cuando la ESP32 se conecta por primera vez, IoT Core auto-registra el certificado y publica un evento
4. Una IoT Rule dispara la Lambda `jitr-provisioning` que:
   - Crea un Thing con el nombre del dispositivo (extraido del CN del cert)
   - Activa el certificado
   - Attacha la policy `smartwaste-dev-sensor-policy`
5. En el siguiente boot (60s en dev, 15 min en produccion), la ESP32 se conecta exitosamente

**Prerequisito:** La infraestructura Terraform debe estar desplegada (`terraform apply`) despues de registrar la CA, ya que necesita el ID del certificado CA para crear la IoT Rule.

```bash
cd provisioning

# Esto genera la CA y la registra en IoT Core
# Parametros: directorio de salida, perfil AWS CLI
./register_ca.sh ./ca-keys personal-classify
```

El script:
1. Genera una clave privada RSA 2048 (`ca.key.pem`) y un certificado autofirmado con `basicConstraints=CA:TRUE` (`ca.cert.pem`)
2. Obtiene un registration code de AWS IoT Core
3. Genera un certificado de verificacion firmado por la CA
4. Registra la CA en IoT Core con auto-registration habilitado

Despues de ejecutar el script:
```bash
# Anotar el CA Certificate ID del output y agregarlo a terraform.tfvars:
cd ../../terraform
echo 'iot_ca_certificate_id = "<CA_ID_DEL_OUTPUT>"' >> terraform.tfvars
terraform apply
```

**IMPORTANTE:** Guarda la carpeta `ca-keys/` en un lugar seguro. La clave privada de la CA (`ca.key.pem`) es lo que permite firmar certificados de dispositivos. Si la perdes, no podes generar mas certificados. Si se filtra, cualquiera podria crear dispositivos que se conecten a tu cuenta.

### Paso 4 — Generar certificado para el dispositivo

Cada ESP32 necesita su propio certificado unico. El `DEVICE_ID` sigue el formato `smartwaste-dev-<GID>`, donde `<GID>` es el ID del contenedor de la Intendencia de Montevideo (por ejemplo `101941`):

```bash
# Seguimos en firmware/provisioning/

./generate_device_cert.sh smartwaste-dev-101941 \
    ./ca-keys/ca.cert.pem \
    ./ca-keys/ca.key.pem \
    ./device-certs/
```

Esto genera dos archivos:
- `device-certs/smartwaste-dev-101941.cert.pem` — Certificado del dispositivo
- `device-certs/smartwaste-dev-101941.key.pem` — Clave privada del dispositivo

El `CN` (Common Name) del certificado es `smartwaste-dev-101941`, que sera el nombre del Thing en AWS IoT Core cuando JITR lo registre automaticamente.

### Paso 5 — Configurar el firmware (menuconfig)

Antes de compilar necesitas configurar las variables especificas de tu dispositivo y tu cuenta AWS:

```bash
cd ..  # volver a firmware/
idf.py menuconfig
```

Navega a **SmartWaste Sensor Configuration** y configura:

#### Container (obligatorio cambiar)

| Setting | Donde encontrarlo | Que poner |
|---------|-------------------|-----------|
| **Container ID** | Container → Container ID | El `gid` del contenedor (ej: `101941`) |
| **Container height (mm)** | Container → Container height in mm | Distancia sensor-fondo cuando esta vacio (ej: `1200`) |
| **Latitude** | Container → Latitude | Coordenada del contenedor (ej: `-34.835566`) |
| **Longitude** | Container → Longitude | Coordenada del contenedor (ej: `-56.243533`) |

#### MQTT (obligatorio cambiar)

| Setting | Donde encontrarlo | Que poner |
|---------|-------------------|-----------|
| **MQTT Broker URI** | MQTT → MQTT Broker URI | Tu endpoint de AWS IoT Core |

Para obtener tu endpoint de IoT Core:

```bash
aws iot describe-endpoint --endpoint-type iot:Data-ATS --profile personal-classify
```

El resultado es algo como `a1b2c3d4e5f6g7.iot.us-east-1.amazonaws.com`. En menuconfig ponerlo con el prefijo `mqtts://`:

```
mqtts://a1b2c3d4e5f6g7.iot.us-east-1.amazonaws.com
```

#### SIM800L (cambiar solo si usas otro operador)

| Setting | Valor por defecto | Notas |
|---------|-------------------|-------|
| **Cellular APN** | `antel.lte` | Cambiar si usas otro operador que no sea Antel |

#### Power Management (opcional)

| Setting | Valor por defecto | Notas |
|---------|-------------------|-------|
| **Deep sleep interval** | `900` (15 min) | Segundos entre mediciones. Menor = mas lecturas pero menos bateria |
| **Max awake time** | `120` (2 min) | Timeout de seguridad: si no completa el ciclo, fuerza deep sleep |

#### Sensors (cambiar solo si usas otros pines)

Los pines por defecto coinciden con el diagrama de wiring de esta guia. Si tu cableado es diferente, ajusta los pines en **Sensors** y **SIM800L**. No hace falta cambiar codigo, solo menuconfig.

### Paso 6 — Crear y flashear la particion NVS con los certificados

La ESP32 guarda los certificados TLS y el container_id en la particion NVS (Non-Volatile Storage). Necesitas crear un archivo CSV que describe el contenido de la particion, generar el binario, y flashearlo:

```bash
# Crear el CSV (ajustar los paths y el container_id)
cat > nvs_data.csv << 'EOF'
key,type,encoding,value
smartwaste,namespace,,
container_id,data,string,101941
dev_cert,file,string,./provisioning/device-certs/smartwaste-dev-101941.cert.pem
dev_key,file,string,./provisioning/device-certs/smartwaste-dev-101941.key.pem
EOF
```

**Que tiene este CSV:**
- `smartwaste` — nombre del namespace NVS (debe coincidir con `NVS_NAMESPACE` en `app_config.h`)
- `container_id` — el ID del contenedor, leido por el firmware en el arranque
- `dev_cert` — certificado TLS del dispositivo (firmado por tu CA)
- `dev_key` — clave privada TLS del dispositivo

Generar el binario y flashearlo:

```bash
# Generar el binario NVS (tamaño 0x10000 = 64KB, debe coincidir con partitions.csv)
python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate nvs_data.csv nvs_data.bin 0x10000

# Flashear la particion NVS (offset 0x9000, definido en partitions.csv)
# Para encontrar tu puerto: ls /dev/cu.usb*
# macOS: /dev/cu.usbserial-XXXX  |  Linux: /dev/ttyUSB0
esptool.py --port /dev/cu.usbserial-0001 write_flash 0x9000 nvs_data.bin
```

### Paso 7 — Compilar y flashear el firmware

```bash
# Compilar
idf.py build

# Flashear firmware + abrir monitor serial
idf.py -p /dev/cu.usbserial-0001 flash monitor
```

El monitor serial muestra todo el ciclo del dispositivo:

```
I (324) main: === SmartWaste Sensor Boot #1 ===
I (334) main: Container ID: 101941
I (520) jsn_sr04t: Initialized on UART1 (TX=17, RX=16)
I (530) vl53l1x: Initialized on I2C0 (SDA=21, SCL=22)
I (1205) main: Fill level: 43.2%
I (1210) main: Battery: 87.3%
I (2800) sim800l: GPRS connected
I (3100) ntp: Time synchronized
I (4500) mqtt: Connected to broker
I (4510) main: Publishing to smartwaste-dev/sensors/101941
I (4520) mqtt: Published to smartwaste-dev/sensors/101941 (msg_id=1, qos=1)
I (4530) main: Cycle complete. Sleeping for 900 seconds...
```

Presiona `Ctrl+]` para salir del monitor.

### Paso 8 — Verificar que funciono

La primera vez que la ESP32 se conecta, JITR tarda ~1 segundo en crear el Thing. Es normal que la **primera conexion** falle con un error TLS (0x8008) — IoT Core auto-registra el certificado y la Lambda JITR lo provisiona. En el siguiente ciclo de deep sleep (60s en dev, 15 min en produccion), la ESP32 se reconecta y funciona sin problemas.

```bash
# Verificar que el Thing fue creado automaticamente por JITR
aws iot describe-thing --thing-name smartwaste-dev-101941 --profile personal-classify

# Verificar que la lectura llego a DynamoDB
aws dynamodb get-item \
    --table-name smartwaste-dev-containers \
    --key '{"container_id": {"S": "101941"}}' \
    --profile personal-classify
```

### Flashear multiples dispositivos

Para cada nuevo contenedor solo cambia la particion NVS (los certificados y el container_id). **El binario del firmware es el mismo para todos los dispositivos**, siempre y cuando compartan el mismo endpoint MQTT y configuracion de pines.

Workflow por dispositivo:

```bash
# 1. Generar certificado unico
./provisioning/generate_device_cert.sh smartwaste-dev-<GID> \
    ./provisioning/ca-keys/ca.cert.pem \
    ./provisioning/ca-keys/ca.key.pem \
    ./provisioning/device-certs/

# 2. Crear CSV con los datos del dispositivo
cat > nvs_data.csv << EOF
key,type,encoding,value
smartwaste,namespace,,
container_id,data,string,<GID>
dev_cert,file,string,./provisioning/device-certs/smartwaste-dev-<GID>.cert.pem
dev_key,file,string,./provisioning/device-certs/smartwaste-dev-<GID>.key.pem
EOF

# 3. Generar binario NVS
python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate nvs_data.csv nvs_data.bin 0x10000

# 4. Flashear NVS + firmware
esptool.py --port /dev/cu.usbserial-0001 write_flash 0x9000 nvs_data.bin
idf.py -p /dev/cu.usbserial-0001 flash
```

Si queres cambiar las coordenadas o la altura del contenedor por dispositivo sin hacer `menuconfig` cada vez, podes agregar esas variables tambien al CSV de NVS y leerlas desde NVS en el firmware (actualmente el firmware lee esos valores de Kconfig, pero `container_id` ya se lee de NVS con fallback a Kconfig).

---

## Troubleshooting

### "Device certificate or key not found in NVS"
La particion NVS no fue flasheada, o las claves del CSV estan mal. Verifica que el CSV tiene el namespace `smartwaste` y las keys `dev_cert` y `dev_key`. Re-flashea la particion NVS (paso 6).

### La primera conexion MQTT falla
Es normal con JITR. La primera vez que un dispositivo desconocido se conecta, AWS IoT Core cierra la conexion TLS mientras auto-registra el certificado. La Lambda JITR se ejecuta en ~1 segundo para crear el Thing y activar el certificado. En el siguiente ciclo de deep sleep (60s en dev, 15 min en produccion), la ESP32 se reconecta y funciona sin problemas.

### SIM800L no responde
- Verificar la fuente de alimentacion: necesita 3.4V–4.4V con un capacitor de 1000uF.
- Verificar el cableado del PWRKEY (necesita un transistor NPN, no conectar directamente al GPIO).
- Verificar que la SIM esta activa y tiene datos habilitados.
- Probar con comandos AT manuales: desconectar la ESP32 y conectar el SIM800L a un conversor USB-Serial, enviar `AT` y verificar que responde `OK`.

### TLS handshake falla (despues de la primera conexion)
- Verificar que el endpoint IoT Core en menuconfig coincide con tu cuenta AWS.
- Verificar que descargaste el Amazon Root CA (paso 2) y que esta en `certs/AmazonRootCA1.pem`.
- Verificar que el certificado del dispositivo fue firmado por la CA registrada en IoT Core.
- En el monitor serial, buscar el codigo de error TLS (ej: `0x7780` = certificado no reconocido).

### No llegan lecturas a DynamoDB
- Verificar en el monitor serial que el publish fue exitoso.
- Verificar que la IoT Rule existe y esta habilitada (definida en Terraform).
- Revisar los logs de la Lambda `process-sensor-reading` en CloudWatch.

### Bateria se agota rapido
- Verificar la corriente en deep sleep con un multimetro (~10uA esperado).
- Verificar que el SIM800L se apaga completamente durante sleep (PWRKEY se toglea).
- Considerar aumentar el intervalo de deep sleep (menuconfig → Power Management).
- Ver [docs/power_budget.md](power_budget.md) para estimaciones detalladas.
