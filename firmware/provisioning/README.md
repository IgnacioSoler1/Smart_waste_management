# JITR — Just-in-Time Registration

Automatic provisioning of ESP32 devices in AWS IoT Core.

## How it works

1. A custom CA (Certificate Authority) is created and registered in AWS IoT Core
2. Each device receives a certificate signed by that CA
3. The first time a device connects to IoT Core, AWS recognizes the CA
4. IoT Core auto-registers the cert and publishes an event
5. An IoT Rule triggers the `jitr-provisioning` Lambda which:
   - Creates a Thing with the certificate's CN as the name
   - Activates the certificate
   - Attaches the `smartwaste-dev-sensor-policy`
   - Attaches the cert to the Thing
6. On the next boot, the device connects successfully and publishes data

## Initial setup (one time only)

```bash
# 1. Register the CA in AWS IoT Core
./register_ca.sh ./ca-keys personal-classify

# Store ca-keys/ in a secure location (NOT in the repo)

# 2. Add CA ID to terraform.tfvars and deploy JITR infrastructure
cd ../../terraform
echo 'iot_ca_certificate_id = "<CA_ID_FROM_SCRIPT_OUTPUT>"' >> terraform.tfvars
terraform apply
```

## Provisioning a new device

```bash
# 1. Generate device certificate
./generate_device_cert.sh smartwaste-dev-101941 ./ca-keys/ca.cert.pem ./ca-keys/ca.key.pem ./device-certs/

# 2. Create CSV file for the NVS partition generator
cat > nvs_data.csv << 'EOF'
key,type,encoding,value
smartwaste,namespace,,
container_id,data,string,101941
dev_cert,file,string,./device-certs/smartwaste-dev-101941.cert.pem
dev_key,file,string,./device-certs/smartwaste-dev-101941.key.pem
EOF

# 3. Generate NVS binary
python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate nvs_data.csv nvs_data.bin 0x10000

# 4. Flash to ESP32
esptool.py --port /dev/ttyUSB0 write_flash 0x9000 nvs_data.bin

# 5. Flash firmware (if not already flashed)
cd ../
idf.py -p /dev/ttyUSB0 flash

# 6. Verify in AWS (after second boot — first boot triggers provisioning)
aws iot describe-thing --thing-name smartwaste-dev-101941 --profile personal-classify
```

## Files

| File | Description |
|------|-------------|
| `register_ca.sh` | Generates a CA and registers it in IoT Core (run once) |
| `generate_device_cert.sh` | Generates a cert + key per device, signed by the CA |
| `provision_batch.sh` | Mass provisioning for 10K+ devices |
| `flash_device.sh` | Flash firmware + NVS to a single device |

## Infrastructure (Terraform)

The JITR Lambda and IoT Rule are managed in `terraform/iot.tf`:
- `aws_lambda_function.jitr_provisioning` — provisions devices on cert registration
- `aws_iot_topic_rule.jitr_provision` — routes cert events to the Lambda

## Security

- The CA private key (`ca.key.pem`) must **never** be committed to the repository
- Device keys are flashed to the ESP32 NVS and are not stored in the repo
- The `ca-keys/` directory is listed in `.gitignore`
- In production, use AWS KMS or an HSM to store the CA key
