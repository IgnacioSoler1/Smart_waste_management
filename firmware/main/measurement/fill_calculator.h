#pragma once

#include "esp_err.h"

// Fill level calculator.
// Converts distance measurements from one or more sensors into a
// fill percentage [0, 100].
//
// Supports sensors mounted at an angle: the measured distance is
// corrected by cos(angle) to get the true vertical distance.

typedef struct {
    float distance_mm;  // Measured distance (after median filter)
    int angle_deg;      // Mount angle from vertical (0 = straight down)
    float weight;       // Weight for weighted average (e.g., 1.0 for vertical, 0.5 for angled)
} fill_sensor_reading_t;

// Calculate fill percentage from one or more sensor readings.
// container_height_mm: distance from sensor to bottom when empty.
// readings: array of sensor readings (distance + angle + weight).
// count: number of readings.
// fill_pct: output fill percentage [0.0, 100.0].
esp_err_t fill_calculator_compute(float container_height_mm,
                                  const fill_sensor_reading_t *readings,
                                  int count,
                                  float *fill_pct);
