#pragma once

#include "esp_err.h"
#include "esp_netif.h"

// SIM800L GSM/GPRS modem driver.
// Handles power control, AT command initialization, and PPP data connection.
//
// The SIM800L is controlled via UART AT commands for setup, then switched
// to PPP mode for data. ESP-IDF's esp_netif PPP layer handles the IP stack,
// giving us a standard network interface for TLS/MQTT.

typedef struct {
    int uart_num;
    int tx_pin;
    int rx_pin;
    int pwrkey_pin;
    int baudrate;
    const char *apn;
} sim800l_config_t;

// Power on the SIM800L module (toggle PWRKEY).
esp_err_t sim800l_power_on(const sim800l_config_t *config);

// Initialize UART and verify AT communication.
esp_err_t sim800l_init(const sim800l_config_t *config);

// Connect to GPRS via PPP. After this call, the ESP32 has an IP address
// and can use standard socket/TLS APIs.
esp_err_t sim800l_connect_ppp(void);

// Disconnect PPP and GPRS.
esp_err_t sim800l_disconnect(void);

// Power off the SIM800L module.
void sim800l_power_off(void);

// Check if PPP is connected and has an IP.
bool sim800l_is_connected(void);
