#pragma once

#include "esp_err.h"

// MQTT client for publishing sensor readings to AWS IoT Core.
// Uses mutual TLS authentication with device certificates stored in NVS.
// Connects via the PPP interface provided by the SIM800L driver.

typedef struct {
    const char *broker_uri;      // mqtts://xxxx.iot.us-east-1.amazonaws.com
    const char *client_id;       // e.g., "smartwaste-dev-101941"
    const char *device_cert_pem; // PEM-encoded device certificate
    const char *device_key_pem;  // PEM-encoded device private key
    const char *root_ca_pem;     // PEM-encoded Amazon Root CA
} mqtt_client_config_t;

// Initialize and connect the MQTT client with TLS mutual auth.
esp_err_t mqtt_client_connect(const mqtt_client_config_t *config);

// Publish a message to the given topic. Blocks until PUBACK (QoS 1).
esp_err_t mqtt_client_publish(const char *topic, const char *payload, int qos);

// Disconnect and clean up the MQTT client.
void mqtt_client_disconnect(void);
