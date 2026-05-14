#pragma once

#include <stdint.h>
#include "esp_err.h"

// Deep sleep manager.
// Configures timer-based wakeup and manages RTC memory for persistent state.

// RTC memory data that survives deep sleep
typedef struct {
    uint32_t boot_count;        // Number of times the device has woken up
    float last_fill_level;      // Last measured fill level (for change detection)
    int64_t last_ntp_sync_us;   // Timestamp of last NTP sync (microseconds since boot)
} rtc_state_t;

// Get pointer to RTC state (persists across deep sleep).
rtc_state_t *sleep_manager_get_state(void);

// Enter deep sleep for the configured interval.
// This function does not return — the device resets on wakeup.
void sleep_manager_enter_deep_sleep(int sleep_seconds);

// Start a safety timer that forces deep sleep if the device stays
// awake too long (e.g., stuck connecting to GPRS).
void sleep_manager_start_watchdog(int max_awake_seconds);
