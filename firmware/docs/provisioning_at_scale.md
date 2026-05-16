# Provisioning at Scale — 10,000+ Devices

This document explains how to provision thousands of ESP32 devices for the SmartWaste MVD deployment.

## Certificate flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        YOUR MACHINE (secure)                         │
│                                                                      │
│  CA private key (ca.key.pem)                                         │
│       │                                                              │
│       ▼                                                              │
│  Signs each device certificate                                       │
│       │                                                              │
│       ├──▶ device-101941.cert.pem + .key.pem ──▶ ESP32 NVS          │
│       ├──▶ device-101942.cert.pem + .key.pem ──▶ ESP32 NVS          │
│       └──▶ ...                                                       │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                        AWS IoT Core                                   │
│                                                                      │
│  CA certificate (ca.cert.pem) — registered with auto-registration    │
│       │                                                              │
│       ▼                                                              │
│  On first TLS handshake from a device:                               │
│    1. Verifies cert was signed by registered CA                      │
│    2. Auto-registers cert (PENDING_ACTIVATION)                       │
│    3. Publishes to $aws/events/certificates/registered/<caCertId>    │
│    4. IoT Rule triggers Lambda (jitr-provisioning):                  │
│       - Creates Thing (name = certificate CN)                        │
│       - Activates the certificate                                    │
│       - Attaches policy 'smartwaste-dev-sensor-policy'               │
│       - Attaches cert to Thing                                       │
│    5. Device reconnects on next boot → publishes data                │
└─────────────────────────────────────────────────────────────────────┘
```

**Key point**: JITR (Just-in-Time Registration) automates the creation of Things and activation of certificates. It does **NOT** create the certificates themselves. Certificates are generated offline using your CA key and flashed to each device's NVS partition. The first connection always fails (expected); provisioning completes within ~1 second via Lambda.

## What goes where

| Data | Storage | Same for all devices? |
|------|---------|----------------------|
| Firmware binary (.bin) | Flash (app partition) | Yes |
| Amazon Root CA (AmazonRootCA1.pem) | Embedded in firmware | Yes |
| Container ID | NVS partition | No — unique per device |
| Device certificate (.cert.pem) | NVS partition | No — unique per device |
| Device private key (.key.pem) | NVS partition | No — unique per device |
| IoT Core endpoint | Kconfig (compiled in) | Yes |
| WiFi/APN settings | Kconfig (compiled in) | Yes |
| Sensor pin config | Kconfig (compiled in) | Yes |

## Batch provisioning workflow

### Prerequisites

1. ESP-IDF environment sourced
2. CA registered in AWS IoT Core (`register_ca.sh` — done once)
3. Containers CSV with WGS84 coordinates (`data/processed/containers_wgs84.csv`)
4. Firmware built (`idf.py build`)

### Step 1: Generate all certificates and NVS binaries

```bash
cd firmware/provisioning

./provision_batch.sh \
    --containers ../../data/processed/containers_wgs84.csv \
    --ca-cert ./ca-keys/ca.cert.pem \
    --ca-key ./ca-keys/ca.key.pem \
    --height 1200 \
    --output ./batch_output/
```

This generates:
- `batch_output/device-certs/smartwaste-dev-{gid}.cert.pem` — one per device
- `batch_output/device-certs/smartwaste-dev-{gid}.key.pem` — one per device
- `batch_output/nvs_binaries/nvs_{gid}.bin` — one per device
- `batch_output/nvs_binaries/manifest.csv` — tracking file

### Step 2: Build the firmware (once)

```bash
cd firmware
idf.py build
```

The same firmware binary is used for all devices.

### Step 3: Flash each device

On the production line, connect each ESP32 and run:

```bash
./provisioning/flash_device.sh --gid 101941 --port /dev/cu.usbserial-0001
```

This flashes both the firmware and the device-specific NVS partition in a single command.

## Time estimates

| Operation | Time per device | Notes |
|-----------|----------------|-------|
| Certificate generation | ~0.5s | RSA 2048 key + signing |
| NVS binary generation | ~0.3s | Python script |
| Flashing (firmware + NVS) | ~25s | At 460800 baud |
| **Total per device** | **~26s** | Plus manual handling time |

For 10,000 devices:
- Cert + NVS generation (batch): ~2.5 hours (single-threaded, can parallelize)
- Flashing: ~72 hours at 26s/device with one station

With 4 flashing stations in parallel: ~18 hours for 10,000 devices.

## Security considerations

### CA private key protection

The CA private key (`ca.key.pem`) is the most sensitive asset. Anyone with this key can create valid device certificates.

**Recommendations:**
- Store in an HSM (Hardware Security Module) for production
- At minimum, keep on an air-gapped machine with encrypted disk
- Never commit to git (already in `.gitignore`)
- Limit access to the provisioning engineer

### Device key protection

Device private keys are stored in the ESP32's NVS partition. For additional security:
- Enable NVS encryption (`CONFIG_NVS_ENCRYPTION=y`) in production
- Use ESP32's eFuse to store the NVS encryption key
- This prevents reading keys via JTAG or flash dump

### Certificate rotation

Device certificates are generated with 10-year validity (3650 days). For production deployments:
- Consider shorter validity (1-2 years) with OTA certificate renewal
- Implement certificate rotation via a shadow/jobs mechanism in IoT Core
- Monitor certificate expiry via IoT Core lifecycle events

## Manifest file format

The manifest CSV tracks all provisioned devices:

```csv
container_id,circuit_id,latitude,longitude,cert_path,key_path,nvs_path
101941,047,-34.835566,-56.243533,./device-certs/smartwaste-dev-101941.cert.pem,...
101942,047,-34.836000,-56.244000,./device-certs/smartwaste-dev-101942.cert.pem,...
```

Use this for:
- Tracking which devices have been provisioned
- Auditing certificate-to-device mapping
- Re-generating NVS if needed (e.g., firmware config changes)
