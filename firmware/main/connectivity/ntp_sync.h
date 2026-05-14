#pragma once

#include "esp_err.h"

// NTP time synchronization.
// Syncs the ESP32 RTC clock via SNTP after GPRS connection.
// Only syncs if the time drift exceeds a threshold (default 30s).

// Initialize SNTP with configured servers. Call after GPRS is connected.
esp_err_t ntp_sync_init(void);

// Perform time synchronization. Blocks until sync completes or timeout.
// Skips sync if drift is within max_drift_sec.
esp_err_t ntp_sync_time(int max_drift_sec);

// Stop SNTP client.
void ntp_sync_deinit(void);
