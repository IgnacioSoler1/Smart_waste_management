#include "outlier_filter.h"

#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "outlier_filter";

#define MAX_READINGS 15
#define READING_DELAY_MS 50

static int float_compare(const void *a, const void *b)
{
    float fa = *(const float *)a;
    float fb = *(const float *)b;
    if (fa < fb) return -1;
    if (fa > fb) return 1;
    return 0;
}

esp_err_t outlier_filter_read_median(const sensor_driver_t *sensor,
                                     int readings_count,
                                     float *median_mm)
{
    if (readings_count < 1 || readings_count > MAX_READINGS) {
        ESP_LOGE(TAG, "Invalid readings_count: %d (must be 1-%d)", readings_count, MAX_READINGS);
        return ESP_ERR_INVALID_ARG;
    }

    float values[MAX_READINGS];
    int valid_count = 0;

    for (int i = 0; i < readings_count; i++) {
        float distance = 0;
        esp_err_t err = sensor->read_distance_mm(&distance);
        if (err == ESP_OK) {
            values[valid_count++] = distance;
        } else {
            ESP_LOGW(TAG, "[%s] Reading %d/%d failed: %s",
                     sensor->name, i + 1, readings_count, esp_err_to_name(err));
        }

        if (i < readings_count - 1) {
            vTaskDelay(pdMS_TO_TICKS(READING_DELAY_MS));
        }
    }

    if (valid_count == 0) {
        ESP_LOGE(TAG, "[%s] All %d readings failed", sensor->name, readings_count);
        return ESP_ERR_TIMEOUT;
    }

    // Sort and take median
    qsort(values, valid_count, sizeof(float), float_compare);
    *median_mm = values[valid_count / 2];

    ESP_LOGI(TAG, "[%s] Median: %.0f mm (%d/%d valid readings)",
             sensor->name, *median_mm, valid_count, readings_count);
    return ESP_OK;
}
