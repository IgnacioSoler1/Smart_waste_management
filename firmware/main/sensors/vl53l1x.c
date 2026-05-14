#include "vl53l1x.h"

#include <string.h>
#include "driver/i2c.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "vl53l1x";

// VL53L1X register addresses (subset for basic ranging)
#define VL53L1X_REG_SOFT_RESET                 0x0000
#define VL53L1X_REG_I2C_SLAVE_DEVICE_ADDRESS   0x0001
#define VL53L1X_REG_MODEL_ID                   0x010F
#define VL53L1X_REG_MODULE_TYPE                0x0110
#define VL53L1X_REG_SYSTEM_START               0x0087
#define VL53L1X_REG_RESULT_RANGE_STATUS        0x0089
#define VL53L1X_REG_RESULT_DISTANCE            0x0096
#define VL53L1X_REG_SYSTEM_INTERRUPT_CLEAR     0x0086
#define VL53L1X_REG_TIMING_BUDGET_A_HI         0x0060
#define VL53L1X_REG_TIMING_BUDGET_B_HI         0x0063
#define VL53L1X_REG_DISTANCE_MODE              0x004D
#define VL53L1X_REG_INTERMEASUREMENT           0x006C

#define VL53L1X_EXPECTED_MODEL_ID   0xEA
#define VL53L1X_READ_TIMEOUT_MS     200
#define VL53L1X_MAX_RANGE_MM        4000
#define VL53L1X_I2C_TIMEOUT_MS      100

static int s_i2c_num = -1;

static esp_err_t vl53l1x_write_reg16(uint16_t reg, uint8_t *data, size_t len)
{
    uint8_t buf[2 + len];
    buf[0] = (reg >> 8) & 0xFF;
    buf[1] = reg & 0xFF;
    memcpy(&buf[2], data, len);

    return i2c_master_write_to_device(s_i2c_num, VL53L1X_I2C_ADDR,
                                      buf, sizeof(buf),
                                      pdMS_TO_TICKS(VL53L1X_I2C_TIMEOUT_MS));
}

static esp_err_t vl53l1x_write_byte(uint16_t reg, uint8_t value)
{
    return vl53l1x_write_reg16(reg, &value, 1);
}

static esp_err_t vl53l1x_read_reg16(uint16_t reg, uint8_t *data, size_t len)
{
    uint8_t reg_buf[2] = { (reg >> 8) & 0xFF, reg & 0xFF };

    return i2c_master_write_read_device(s_i2c_num, VL53L1X_I2C_ADDR,
                                        reg_buf, 2, data, len,
                                        pdMS_TO_TICKS(VL53L1X_I2C_TIMEOUT_MS));
}

static esp_err_t vl53l1x_read_byte(uint16_t reg, uint8_t *value)
{
    return vl53l1x_read_reg16(reg, value, 1);
}

static esp_err_t vl53l1x_read_word(uint16_t reg, uint16_t *value)
{
    uint8_t buf[2];
    esp_err_t err = vl53l1x_read_reg16(reg, buf, 2);
    if (err == ESP_OK) {
        *value = ((uint16_t)buf[0] << 8) | buf[1];
    }
    return err;
}

static esp_err_t vl53l1x_wait_boot(void)
{
    for (int i = 0; i < 100; i++) {
        uint8_t state = 0;
        esp_err_t err = vl53l1x_read_byte(0x00E5, &state);
        if (err == ESP_OK && state != 0) {
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    ESP_LOGE(TAG, "Boot timeout");
    return ESP_ERR_TIMEOUT;
}

// Default configuration for long distance mode (from ST ULD)
static const uint8_t vl53l1x_default_config[] = {
    0x00, 0x00, 0x00, 0x01, 0x02, 0x00, 0x02, 0x08,
    0x00, 0x08, 0x10, 0x01, 0x01, 0x00, 0x00, 0x00,
    0x00, 0xFF, 0x00, 0x0F, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x20, 0x0B, 0x00, 0x00, 0x02, 0x14, 0x21,
    0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0xC8,
    0x00, 0x00, 0x38, 0xFF, 0x01, 0x00, 0x08, 0x00,
    0x00, 0x01, 0xCC, 0x0F, 0x01, 0xF1, 0x0D, 0x01,
    0x68, 0x00, 0x80, 0x08, 0xB8, 0x00, 0x00, 0x00,
    0x00, 0x0F, 0x89, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x01, 0x0F, 0x0D, 0x0E, 0x0E, 0x00,
    0x00, 0x02, 0xC7, 0xFF, 0x9B, 0x00, 0x00, 0x00,
    0x01, 0x03, 0x00,
};

esp_err_t vl53l1x_init(const sensor_config_t *config)
{
    s_i2c_num = config->port_num;

    i2c_config_t i2c_cfg = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = config->pin_a,
        .scl_io_num = config->pin_b,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = 400000,
    };

    esp_err_t err = i2c_param_config(s_i2c_num, &i2c_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C config failed: %s", esp_err_to_name(err));
        return err;
    }

    err = i2c_driver_install(s_i2c_num, I2C_MODE_MASTER, 0, 0, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C driver install failed: %s", esp_err_to_name(err));
        return err;
    }

    // Software reset
    err = vl53l1x_write_byte(VL53L1X_REG_SOFT_RESET, 0x00);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Soft reset failed — sensor not connected?");
        i2c_driver_delete(s_i2c_num);
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(1));
    vl53l1x_write_byte(VL53L1X_REG_SOFT_RESET, 0x01);

    err = vl53l1x_wait_boot();
    if (err != ESP_OK) {
        i2c_driver_delete(s_i2c_num);
        return err;
    }

    // Verify model ID
    uint8_t model_id = 0;
    vl53l1x_read_byte(VL53L1X_REG_MODEL_ID, &model_id);
    if (model_id != VL53L1X_EXPECTED_MODEL_ID) {
        ESP_LOGE(TAG, "Unexpected model ID: 0x%02X (expected 0x%02X)", model_id, VL53L1X_EXPECTED_MODEL_ID);
        i2c_driver_delete(s_i2c_num);
        return ESP_ERR_NOT_FOUND;
    }

    // Load default configuration (long distance mode)
    err = vl53l1x_write_reg16(0x002D, (uint8_t *)vl53l1x_default_config, sizeof(vl53l1x_default_config));
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to load default config");
        i2c_driver_delete(s_i2c_num);
        return err;
    }

    // Set inter-measurement period to 100ms
    uint8_t im_period[4] = { 0x00, 0x00, 0x00, 0x64 };
    vl53l1x_write_reg16(VL53L1X_REG_INTERMEASUREMENT, im_period, 4);

    ESP_LOGI(TAG, "Initialized on I2C%d (SDA=%d, SCL=%d), model=0x%02X",
             s_i2c_num, config->pin_a, config->pin_b, model_id);
    return ESP_OK;
}

esp_err_t vl53l1x_read_distance_mm(float *distance_mm)
{
    if (s_i2c_num < 0) {
        return ESP_ERR_INVALID_STATE;
    }

    // Start single-shot measurement
    esp_err_t err = vl53l1x_write_byte(VL53L1X_REG_SYSTEM_START, 0x40);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start measurement");
        return err;
    }

    // Wait for data ready
    for (int i = 0; i < VL53L1X_READ_TIMEOUT_MS / 5; i++) {
        uint8_t ready = 0;
        vl53l1x_read_byte(0x0030, &ready);
        if (ready & 0x01) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
        if (i == (VL53L1X_READ_TIMEOUT_MS / 5) - 1) {
            ESP_LOGE(TAG, "Measurement timeout");
            return ESP_ERR_TIMEOUT;
        }
    }

    // Read range status
    uint8_t range_status = 0;
    vl53l1x_read_byte(VL53L1X_REG_RESULT_RANGE_STATUS, &range_status);
    range_status &= 0x1F;

    // Read distance
    uint16_t distance = 0;
    err = vl53l1x_read_word(VL53L1X_REG_RESULT_DISTANCE, &distance);
    if (err != ESP_OK) {
        return err;
    }

    // Clear interrupt
    vl53l1x_write_byte(VL53L1X_REG_SYSTEM_INTERRUPT_CLEAR, 0x01);

    // Stop ranging
    vl53l1x_write_byte(VL53L1X_REG_SYSTEM_START, 0x00);

    // Check range status (9 = valid measurement)
    if (range_status != 9) {
        ESP_LOGW(TAG, "Range status %d (not valid), distance=%u mm", range_status, distance);
        return ESP_ERR_INVALID_RESPONSE;
    }

    if (distance > VL53L1X_MAX_RANGE_MM) {
        ESP_LOGW(TAG, "Out of range: %u mm", distance);
        return ESP_ERR_INVALID_RESPONSE;
    }

    *distance_mm = (float)distance;
    ESP_LOGD(TAG, "Distance: %.0f mm (status=%d)", *distance_mm, range_status);
    return ESP_OK;
}

void vl53l1x_deinit(void)
{
    if (s_i2c_num >= 0) {
        i2c_driver_delete(s_i2c_num);
        s_i2c_num = -1;
        ESP_LOGI(TAG, "Deinitialized");
    }
}

const sensor_driver_t vl53l1x_driver = {
    .name = "VL53L1X",
    .init = vl53l1x_init,
    .read_distance_mm = vl53l1x_read_distance_mm,
    .deinit = vl53l1x_deinit,
};
