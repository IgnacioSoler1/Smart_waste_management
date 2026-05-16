# SmartWaste MVD — ESP32 Sensor Firmware

ESP-IDF firmware for waste container fill-level sensors. Measures fill level using ultrasonic (JSN-SR04T) and ToF laser (VL53L1X) sensors, publishes readings to AWS IoT Core via GPRS (SIM800L), and sleeps in deep sleep between cycles.

## Deployment modes

The firmware supports two connectivity modes, configured at compile time via Kconfig:

| Mode | Connectivity | Sensors | Sleep | Use case |
|------|-------------|---------|-------|----------|
| **Dev** | WiFi | Simulated (random) | 60s | Testing pipeline without hardware |
| **Production** | GPRS (SIM800L) | Real (JSN-SR04T + VL53L1X) | 15 min | Field deployment |

Both modes publish to the same MQTT topic and use the same certificate-based TLS auth. The full pipeline (IoT Core → Lambda → DynamoDB) works identically regardless of mode.

### Quick start — Dev mode (no hardware needed)

Only requires an ESP32 dev board and WiFi:

```bash
source "$HOME/.espressif/tools/activate_idf_v6.0.1.sh"
cd firmware
idf.py set-target esp32

# Build with dev defaults (WiFi + simulated sensors)
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.dev" build

# Configure your WiFi and IoT Core endpoint
idf.py menuconfig
# → Connectivity Mode → WiFi SSID / Password
# → MQTT → Broker URI

# Flash NVS with certificates (same as production — see step 5 below)
# Flash firmware
idf.py -p /dev/cu.usbserial-0001 flash monitor
```

### Batch provisioning (10,000+ devices)

For mass deployment, use the provisioning scripts to generate per-device certificates and NVS binaries from the containers CSV:

```bash
cd provisioning
./provision_batch.sh \
    --containers ../../data/processed/containers_wgs84.csv \
    --ca-cert ./ca-keys/ca.cert.pem \
    --ca-key ./ca-keys/ca.key.pem \
    --output ./batch_output/

# Flash individual device
./flash_device.sh --gid 101941 --port /dev/cu.usbserial-0001
```

See [docs/provisioning_at_scale.md](docs/provisioning_at_scale.md) for the full guide.

## How certificates and JITR work

```
CA key (your machine) ──signs──▶ device certificate ──▶ ESP32 NVS
                                                              │
                                                     first TLS handshake
                                                              │
                                                              ▼
CA cert (AWS IoT Core) ◀──verifies chain──────────────────────┘
                              │
                    auto-registers cert (PENDING_ACTIVATION)
                              │
                    publishes to $aws/events/certificates/registered/<caCertId>
                              │
                              ▼
                    IoT Rule → Lambda (JITR provisioning):
                       • Creates Thing (with ThingType)
                       • Activates cert
                       • Attaches policy
                       • Attaches cert to Thing
                              │
                              ▼
                    second boot → MQTT connects OK → publishes data
```

**Important**: JITR (Just-in-Time Registration) automates the creation of Things in AWS IoT Core. It does **not** create certificates. Certificates are generated offline using your CA private key and pre-flashed into each device's NVS partition. The first connection always fails (expected); provisioning completes within ~1 second, and the device succeeds on its next boot.

## Prerequisites

- **ESP-IDF v5.1+** (tested with v6.0.1) installed and configured ([install guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/))
- **ESP32-WROOM-32** dev board (ESP32-S2/S3 not supported)
- **SIM800L** module with GSM antenna and active SIM card (production mode only)
- **JSN-SR04T** ultrasonic sensor (UART mode) (production mode only)
- **VL53L1X** ToF laser sensor (production mode only)
- **Python 3.8+** (for ESP-IDF tools and NVS partition generator)
- **AWS CLI v2** configured with IoT Core permissions
- **openssl** (for certificate generation)

See [docs/hardware_setup.md](docs/hardware_setup.md) for the full wiring diagram and component list.

## Quick start (production mode)

### 1. Set up ESP-IDF environment

```bash
# If installed with eim (Espressif Installation Manager):
source "$HOME/.espressif/tools/activate_idf_v6.0.1.sh"

# If installed manually (git clone):
# . $HOME/esp/esp-idf/export.sh

# Set target to ESP32
cd firmware
idf.py set-target esp32
```

> **Note (ESP-IDF v6.0+):** The `mqtt` component was removed from the ESP-IDF core
> and must be pulled via the Component Manager. This is already configured in
> `main/idf_component.yml` (`espressif/mqtt`). The `managed_components/` directory
> is created automatically on the first build — do not commit it to git.

### 2. Configure the firmware

```bash
idf.py menuconfig
```

Navigate to **SmartWaste Sensor Configuration** and set:

| Setting | Menu path | Description |
|---------|-----------|-------------|
| Container ID | Container → Container ID | The `gid` from Intendencia data (e.g., `101941`) |
| Container height | Container → Container height in mm | Distance from sensor to bottom when empty |
| Latitude / Longitude | Container → Latitude / Longitude | Container coordinates in decimal degrees (WGS84) |
| IoT Core endpoint | MQTT → MQTT Broker URI | Your AWS IoT endpoint (`mqtts://xxxx.iot.us-east-1.amazonaws.com`) |
| APN | SIM800L → Cellular APN | SIM card provider APN (default: `antel.lte` for Antel Uruguay) |
| Sleep interval | Power Management → Deep sleep interval | Seconds between measurements (default: 900 = 15 min) |

For a full list of settings, see [main/Kconfig.projbuild](main/Kconfig.projbuild).

> **Tip**: Find your IoT Core endpoint with:
> ```bash
> aws iot describe-endpoint --endpoint-type iot:Data-ATS --profile personal-classify
> ```

### 3. Set up JITR and generate device certificates

```bash
cd provisioning

# One-time setup: create a CA and register it in AWS IoT Core
./register_ca.sh ./ca-keys personal-classify

# Add the CA ID (from script output) to terraform.tfvars and deploy JITR infrastructure
cd ../../terraform
echo 'iot_ca_certificate_id = "<CA_ID>"' >> terraform.tfvars
terraform apply

# Generate a certificate for this device
cd ../firmware/provisioning
./generate_device_cert.sh smartwaste-dev-101941 ./ca-keys/ca.cert.pem ./ca-keys/ca.key.pem ./device-certs/
```

See [provisioning/README.md](provisioning/README.md) for detailed instructions.

### 4. Download the Amazon Root CA

This file is embedded into the firmware binary at compile time (via `EMBED_TXTFILES` in
`main/CMakeLists.txt`), so you **must** download it before running `idf.py build`:

```bash
curl -o certs/AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
```

### 5. Flash certificates to NVS

Create a CSV file describing the NVS contents:

```bash
cat > nvs_data.csv << 'EOF'
key,type,encoding,value
smartwaste,namespace,,
container_id,data,string,101941
dev_cert,file,string,./provisioning/device-certs/smartwaste-dev-101941.cert.pem
dev_key,file,string,./provisioning/device-certs/smartwaste-dev-101941.key.pem
root_ca,file,string,./certs/AmazonRootCA1.pem
EOF
```

Generate and flash the NVS partition:

```bash
# Generate binary
python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate nvs_data.csv nvs_data.bin 0x10000

# Flash NVS partition (0x9000 matches partitions.csv offset)
esptool.py --port /dev/cu.usbserial-0001 write_flash 0x9000 nvs_data.bin
```

### 6. Build and flash the firmware

```bash
cd ..  # back to firmware/
idf.py build
idf.py -p /dev/cu.usbserial-0001 flash monitor
```

The monitor will show the device booting, reading sensors, connecting to GPRS, and publishing to MQTT. Press `Ctrl+]` to exit the monitor.

### 7. Verify end-to-end

```bash
# Check the Thing was auto-created in AWS IoT Core (JITR)
aws iot describe-thing --thing-name smartwaste-dev-101941 --profile personal-classify

# Check sensor readings arrived in DynamoDB
aws dynamodb get-item \
    --table-name smartwaste-dev-containers \
    --key '{"container_id": {"S": "101941"}}' \
    --profile personal-classify
```

## Customizing for your setup

### Changing sensor pins

If your wiring differs from the defaults, update the pin assignments in `idf.py menuconfig` under **SmartWaste Sensor Configuration → Sensors** and **SIM800L**. No code changes are needed.

### Using only one sensor

The firmware works with either sensor alone. If a sensor fails to initialize, it is skipped automatically. The fill level is calculated from whichever sensors are available.

To permanently disable a sensor, you can skip wiring it — the firmware will log a warning and continue with the other sensor.

### Changing the measurement interval

In `idf.py menuconfig`, go to **Power Management → Deep sleep interval** and set the desired value in seconds. See [docs/power_budget.md](docs/power_budget.md) for battery life estimates at 15, 20, and 30-minute intervals.

### Changing the APN

In `idf.py menuconfig`, go to **SIM800L → Cellular APN**. The default `antel.lte` works for Antel Uruguay. For other providers, set the appropriate APN.

### Custom container dimensions

Set **Container → Container height in mm** to the distance from the sensor to the bottom of the container when empty. This is the reference distance used to calculate fill percentage.

If sensors are mounted at an angle (not straight down), set the mount angle under **Sensors**. The firmware corrects the measured distance using `cos(angle)`.

### Deploying multiple devices

For each new device:

1. Generate a unique certificate: `./provisioning/generate_device_cert.sh smartwaste-dev-<GID>`
2. Create an `nvs_data.csv` with the device's `container_id`, cert, and key paths
3. Generate and flash the NVS partition
4. Flash the firmware (same binary for all devices)
5. On first boot, JITR creates the Thing automatically in AWS IoT Core

The firmware binary is the same for all devices — only the NVS partition differs (container_id, coordinates, certificates). You can pre-build the firmware once and flash it to all devices.

## Project structure

```
firmware/
├── CMakeLists.txt              # ESP-IDF root project
├── sdkconfig.defaults          # Default config (deep sleep, PPP, TLS)
├── sdkconfig.defaults.dev      # Dev mode overrides (WiFi + simulated sensors)
├── partitions.csv              # Partition table (64KB NVS for certs)
├── main/
│   ├── CMakeLists.txt          # Component registration
│   ├── main.c                  # Entry point: init → measure → publish → sleep
│   ├── Kconfig.projbuild       # menuconfig options (incl. connectivity mode)
│   ├── app_config.h            # Configuration constants
│   ├── idf_component.yml       # esp_modem dependency
│   ├── sensors/                # Sensor drivers
│   │   ├── sensor_interface.h  # Abstract interface
│   │   ├── jsn_sr04t.c/.h      # Ultrasonic (UART)
│   │   └── vl53l1x.c/.h       # ToF laser (I2C)
│   ├── measurement/            # Signal processing
│   │   ├── fill_calculator.c/.h
│   │   └── outlier_filter.c/.h
│   ├── connectivity/           # Network stack
│   │   ├── sim800l.c/.h        # GSM/GPRS modem (PPP, production)
│   │   ├── wifi_sta.c/.h       # WiFi station mode (dev)
│   │   ├── sw_mqtt_client.c/.h # MQTT + TLS to AWS IoT Core
│   │   └── ntp_sync.c/.h       # Time synchronization
│   └── power/
│       └── sleep_manager.c/.h  # Deep sleep + RTC memory
├── certs/                      # Dev certificates (gitignored)
├── provisioning/               # JITR + batch provisioning scripts
│   ├── generate_device_cert.sh # Generate cert for one device
│   ├── register_ca.sh          # Register CA in IoT Core (one-time)
│   ├── provision_batch.sh      # Mass provisioning (10K+ devices)
│   ├── flash_device.sh         # Flash firmware + NVS to one device
│   └── README.md
└── docs/
    ├── hardware_setup.md       # Wiring diagram + flashing guide
    ├── provisioning_at_scale.md # Mass provisioning guide
    └── power_budget.md         # Battery life estimates
```

## Troubleshooting

### "Device certificate or key not found in NVS"
The NVS partition was not flashed or the keys are wrong. Re-flash the NVS partition (step 5).

### SIM800L not responding
- Check power supply: SIM800L needs 3.4V–4.4V with a 1000μF capacitor.
- Check the PWRKEY wiring (needs an NPN transistor, not direct GPIO).
- Verify the SIM card is active and has data enabled.

### MQTT TLS handshake fails
- Verify the IoT Core endpoint in menuconfig matches your AWS account.
- Check that the Amazon Root CA was flashed to NVS.
- Ensure the device certificate was signed by the registered CA.

### No sensor readings
- JSN-SR04T: Ensure the jumper is set to UART mode (not trigger/echo).
- VL53L1X: Check I2C pull-up resistors (4.7kΩ to 3.3V).
- Both: Verify wiring matches the pin configuration in menuconfig.

### High battery drain
- Verify deep sleep current with a multimeter (~10μA expected).
- Check that the SIM800L is fully powered off during sleep (PWRKEY toggled).
- See [docs/power_budget.md](docs/power_budget.md) for estimates and optimization tips.
