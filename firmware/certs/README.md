# Certificados de desarrollo

Este directorio es para certificados de desarrollo local.
**No commitear certificados ni keys privadas a git.**

## Archivos necesarios

Para desarrollo local, necesitás:

1. **Amazon Root CA** (`AmazonRootCA1.pem`):
   ```bash
   curl -o AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
   ```

2. **Device certificate** y **private key**: generados con los scripts de provisioning.
   ```bash
   cd ../provisioning
   ./generate_device_cert.sh smartwaste-dev-000000 ./ca-keys/ca.cert.pem ./ca-keys/ca.key.pem ../certs/
   ```

## Flashear certificados al ESP32

Los certificados se almacenan en la partición NVS del ESP32. Ver `../provisioning/README.md` para instrucciones detalladas.
