#include "fill_calculator.h"

#include <math.h>
#include "esp_log.h"

static const char *TAG = "fill_calc";

// Clamp value to [min, max]
static float clampf(float value, float min, float max)
{
    if (value < min) return min;
    if (value > max) return max;
    return value;
}

esp_err_t fill_calculator_compute(float container_height_mm,
                                  const fill_sensor_reading_t *readings,
                                  int count,
                                  float *fill_pct)
{
    if (container_height_mm <= 0 || count <= 0 || readings == NULL || fill_pct == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    float weighted_sum = 0;
    float weight_total = 0;

    for (int i = 0; i < count; i++) {
        float distance = readings[i].distance_mm;
        int angle = readings[i].angle_deg;
        float weight = readings[i].weight;

        // Correct for mount angle: vertical_distance = measured * cos(angle)
        if (angle > 0 && angle < 90) {
            float rad = (float)angle * (float)M_PI / 180.0f;
            distance = distance * cosf(rad);
        }

        // Fill % = (height - distance) / height * 100
        float pct = (container_height_mm - distance) / container_height_mm * 100.0f;
        pct = clampf(pct, 0.0f, 100.0f);

        ESP_LOGD(TAG, "Sensor %d: dist=%.0fmm angle=%d° → vertical=%.0fmm → fill=%.1f%%",
                 i, readings[i].distance_mm, angle, distance, pct);

        weighted_sum += pct * weight;
        weight_total += weight;
    }

    *fill_pct = weighted_sum / weight_total;
    *fill_pct = clampf(*fill_pct, 0.0f, 100.0f);

    ESP_LOGI(TAG, "Fill level: %.1f%% (from %d sensors)", *fill_pct, count);
    return ESP_OK;
}
