#pragma once

#include "esp_err.h"
#include <stdbool.h>

/**
 * Initialize WiFi in station mode and connect to the configured AP.
 * Blocks until an IP address is obtained or timeout occurs.
 *
 * @param ssid     WiFi network name
 * @param password WiFi password (empty string for open networks)
 * @return ESP_OK on successful connection, ESP_FAIL on timeout
 */
esp_err_t sw_wifi_init(const char *ssid, const char *password);

/**
 * Disconnect from WiFi and release resources.
 */
void sw_wifi_disconnect(void);

/**
 * Check if WiFi is connected and has an IP address.
 */
bool sw_wifi_is_connected(void);
