# JITP — Just In Time Provisioning

Provisioning automático de dispositivos ESP32 en AWS IoT Core.

## Cómo funciona

1. Se crea una CA (Certificate Authority) propia y se registra en AWS IoT Core
2. Cada dispositivo recibe un certificado firmado por esa CA
3. La primera vez que un dispositivo se conecta a IoT Core, AWS reconoce la CA
4. IoT Core ejecuta automáticamente el template JITP:
   - Crea una Thing con el nombre del CN del certificado
   - Activa el certificado
   - Adjunta la política `smartwaste-dev-sensor-policy`

## Setup inicial (una sola vez)

```bash
# 1. Registrar la CA en AWS IoT Core
./register_ca.sh ./ca-keys personal-classify

# Guardar ca-keys/ en un lugar seguro (NO en el repo)
```

## Provisionar un dispositivo nuevo

```bash
# 1. Generar certificado del dispositivo
./generate_device_cert.sh smartwaste-dev-101941 ./ca-keys/ca.cert.pem ./ca-keys/ca.key.pem ./device-certs/

# 2. Crear archivo CSV para NVS partition generator
cat > nvs_data.csv << 'EOF'
key,type,encoding,value
smartwaste,namespace,,
container_id,data,string,101941
dev_cert,file,string,./device-certs/smartwaste-dev-101941.cert.pem
dev_key,file,string,./device-certs/smartwaste-dev-101941.key.pem
EOF

# 3. Generar binario NVS
python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate nvs_data.csv nvs_data.bin 0x10000

# 4. Flash al ESP32
esptool.py --port /dev/ttyUSB0 write_flash 0x9000 nvs_data.bin

# 5. Flash firmware (si no está ya)
cd ../
idf.py -p /dev/ttyUSB0 flash

# 6. Verificar en AWS
aws iot describe-thing --thing-name smartwaste-dev-101941 --profile personal-classify
```

## Archivos

| Archivo | Descripción |
|---------|-------------|
| `register_ca.sh` | Genera CA y la registra en IoT Core (ejecutar una vez) |
| `generate_device_cert.sh` | Genera cert+key por dispositivo, firmado por la CA |
| `jitp_template.json` | Template que IoT Core usa para crear Thing+Cert+Policy |

## Seguridad

- La CA private key (`ca.key.pem`) **nunca** debe estar en el repositorio
- Los device keys se flashean al NVS del ESP32 y no se guardan en el repo
- El directorio `ca-keys/` está en `.gitignore`
- En producción, usar AWS KMS o HSM para almacenar la CA key
