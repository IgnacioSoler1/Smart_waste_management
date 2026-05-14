#include "ntp_sync.h"

#include <time.h>
#include <sys/time.h>
#include "esp_log.h"
#include "esp_sntp.h"
#include "app_config.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ntp_sync";

#define NTP_SYNC_TIMEOUT_MS  15000
#define NTP_POLL_INTERVAL_MS 100

// Last sync time stored in RTC memory (survives deep sleep)
static RTC_DATA_ATTR time_t s_last_sync_time = 0;

esp_err_t ntp_sync_init(void)
{
    if (esp_sntp_enabled()) {
        esp_sntp_stop();
    }

    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, APP_NTP_SERVER_PRIMARY);
    esp_sntp_setservername(1, APP_NTP_SERVER_SECONDARY);
    esp_sntp_init();

    ESP_LOGI(TAG, "SNTP initialized (servers: %s, %s)",
             APP_NTP_SERVER_PRIMARY, APP_NTP_SERVER_SECONDARY);
    return ESP_OK;
}

esp_err_t ntp_sync_time(int max_drift_sec)
{
    // Check if we actually need to sync
    time_t now;
    time(&now);

    if (s_last_sync_time > 0 && now > 0) {
        time_t drift = (now > s_last_sync_time) ? (now - s_last_sync_time) : 0;
        // Rough check: if we synced recently and RTC hasn't drifted too much, skip
        // RTC crystal drift is ~150ppm, so in 15 min we drift ~135ms — well within threshold
        if (drift < (max_drift_sec * 10)) { // Only skip if we synced within ~5 minutes ago
            // Actually, we should always sync if the time is not set (epoch)
            if (now > 1700000000) { // After ~Nov 2023, time looks valid
                ESP_LOGI(TAG, "Time looks valid (last sync %lld sec ago), skipping sync", (long long)drift);
                return ESP_OK;
            }
        }
    }

    ESP_LOGI(TAG, "Synchronizing time...");

    int elapsed = 0;
    while (esp_sntp_get_sync_status() != SNTP_SYNC_STATUS_COMPLETED) {
        vTaskDelay(pdMS_TO_TICKS(NTP_POLL_INTERVAL_MS));
        elapsed += NTP_POLL_INTERVAL_MS;
        if (elapsed >= NTP_SYNC_TIMEOUT_MS) {
            ESP_LOGE(TAG, "Sync timeout after %d ms", elapsed);
            return ESP_ERR_TIMEOUT;
        }
    }

    time(&now);
    s_last_sync_time = now;

    struct tm timeinfo;
    gmtime_r(&now, &timeinfo);
    char buf[64];
    strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &timeinfo);
    ESP_LOGI(TAG, "Time synced: %s", buf);

    return ESP_OK;
}

void ntp_sync_deinit(void)
{
    if (esp_sntp_enabled()) {
        esp_sntp_stop();
        ESP_LOGI(TAG, "SNTP stopped");
    }
}
