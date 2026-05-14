#pragma once

#include "sensor_interface.h"

// JSN-SR04T waterproof ultrasonic distance sensor driver.
// Uses UART mode (more stable than trigger/echo in metal containers).
//
// Protocol (UART mode):
//   Send: 0x55
//   Recv: 4 bytes [0xFF, HIGH, LOW, CHECKSUM]
//   Distance (mm) = (HIGH << 8) | LOW
//   Checksum = (0xFF + HIGH + LOW) & 0xFF
//
// Range: 250mm - 4500mm
// Accuracy: +/- 10mm

esp_err_t jsn_sr04t_init(const sensor_config_t *config);
esp_err_t jsn_sr04t_read_distance_mm(float *distance_mm);
void jsn_sr04t_deinit(void);
