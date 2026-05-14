# Development certificates

This directory holds certificates for local development.
**Do not commit certificates or private keys to git.**

## Required files

For local development you need:

1. **Amazon Root CA** (`AmazonRootCA1.pem`):
   ```bash
   curl -o AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
   ```

2. **Device certificate** and **private key**: generated with the provisioning scripts.
   ```bash
   cd ../provisioning
   ./generate_device_cert.sh smartwaste-dev-000000 ./ca-keys/ca.cert.pem ./ca-keys/ca.key.pem ../certs/
   ```

## Flashing certificates to the ESP32

Certificates are stored in the ESP32's NVS partition. See `../provisioning/README.md` for detailed instructions.
