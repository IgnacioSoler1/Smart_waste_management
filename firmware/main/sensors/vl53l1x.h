#pragma once

#include "sensor_interface.h"

// VL53L1X Time-of-Flight laser distance sensor driver.
// Uses I2C communication at 400kHz.
//
// This is a simplified driver that uses the sensor's default configuration.
// For production, consider using ST's full ULD (Ultra Lite Driver) library.
//
// Default I2C address: 0x29
// Range: up to 4000mm (long distance mode)
// Accuracy: +/- 20mm typical

#define VL53L1X_I2C_ADDR  0x29

esp_err_t vl53l1x_init(const sensor_config_t *config);
esp_err_t vl53l1x_read_distance_mm(float *distance_mm);
void vl53l1x_deinit(void);
