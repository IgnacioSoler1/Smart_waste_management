#include "sleep_manager.h"

#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "sleep_mgr";

// RTC memory — persists across deep sleep, reset on power cycle
static RTC_DATA_ATTR rtc_state_t s_rtc_state = {
    .boot_count = 0,
    .last_fill_level = -1.0f,
    .last_ntp_sync_us = 0,
};

static esp_timer_handle_t s_watchdog_timer = NULL;

rtc_state_t *sleep_manager_get_state(void)
{
    return &s_rtc_state;
}

static void watchdog_timeout_cb(void *arg)
{
    int sleep_sec = (int)(intptr_t)arg;
    ESP_LOGW(TAG, "Watchdog timeout! Forcing deep sleep for %d seconds", sleep_sec);
    sleep_manager_enter_deep_sleep(sleep_sec);
}

void sleep_manager_enter_deep_sleep(int sleep_seconds)
{
    // Stop watchdog timer if running
    if (s_watchdog_timer != NULL) {
        esp_timer_stop(s_watchdog_timer);
        esp_timer_delete(s_watchdog_timer);
        s_watchdog_timer = NULL;
    }

    uint64_t sleep_us = (uint64_t)sleep_seconds * 1000000ULL;

    ESP_LOGI(TAG, "Entering deep sleep for %d seconds (boot #%lu)",
             sleep_seconds, (unsigned long)s_rtc_state.boot_count);

    esp_sleep_enable_timer_wakeup(sleep_us);
    esp_deep_sleep_start();
    // Does not return — device resets on wakeup
}

void sleep_manager_start_watchdog(int max_awake_seconds)
{
    if (s_watchdog_timer != NULL) {
        esp_timer_stop(s_watchdog_timer);
        esp_timer_delete(s_watchdog_timer);
    }

    // Pass the default sleep seconds via the callback argument
    // (we use the same default sleep interval from Kconfig)
    esp_timer_create_args_t timer_args = {
        .callback = watchdog_timeout_cb,
        .arg = (void *)(intptr_t)900, // default 15 min, overridden by caller if needed
        .name = "awake_watchdog",
    };

    esp_err_t err = esp_timer_create(&timer_args, &s_watchdog_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create watchdog timer: %s", esp_err_to_name(err));
        return;
    }

    err = esp_timer_start_once(s_watchdog_timer, (uint64_t)max_awake_seconds * 1000000ULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start watchdog timer: %s", esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "Watchdog started: %d seconds max awake time", max_awake_seconds);
    }
}
