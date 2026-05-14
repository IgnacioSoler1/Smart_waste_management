#pragma once

#include "esp_err.h"
#include "sensors/sensor_interface.h"

// Outlier filter using median of N consecutive readings.
// Takes multiple readings from a sensor, sorts them, and returns the median.
// This rejects transient noise (e.g., a bird flying through the beam).

// Take N readings from a sensor and return the median value.
// readings_count should be odd (3, 5, 7...) for a clean median.
esp_err_t outlier_filter_read_median(const sensor_driver_t *sensor,
                                     int readings_count,
                                     float *median_mm);
