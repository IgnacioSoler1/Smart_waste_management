#pragma once

#include "esp_err.h"

// Abstract sensor interface for distance measurement sensors.
// Each sensor driver implements this interface so the main application
// can work with any combination of sensors generically.

typedef struct {
    int pin_a;      // Primary pin (TX for UART, SDA for I2C)
    int pin_b;      // Secondary pin (RX for UART, SCL for I2C)
    int port_num;   // Peripheral number (UART num, I2C num)
    int angle_deg;  // Mount angle from vertical (0 = straight down)
} sensor_config_t;

typedef struct {
    const char *name;
    esp_err_t (*init)(const sensor_config_t *config);
    esp_err_t (*read_distance_mm)(float *distance_mm);
    void (*deinit)(void);
} sensor_driver_t;

// Sensor driver instances (defined in their respective .c files)
extern const sensor_driver_t jsn_sr04t_driver;
extern const sensor_driver_t vl53l1x_driver;
