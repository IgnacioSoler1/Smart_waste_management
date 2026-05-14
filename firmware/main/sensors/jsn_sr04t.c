#include "jsn_sr04t.h"

#include <string.h>
#include "driver/uart.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "jsn_sr04t";

#define JSN_TRIGGER_BYTE   0x55
#define JSN_RESPONSE_LEN   4
#define JSN_HEADER_BYTE    0xFF
#define JSN_READ_TIMEOUT_MS 100
#define JSN_MIN_RANGE_MM   250
#define JSN_MAX_RANGE_MM   4500
#define JSN_UART_BUF_SIZE  256
#define JSN_MAX_RETRIES    3

static int s_uart_num = -1;

esp_err_t jsn_sr04t_init(const sensor_config_t *config)
{
    s_uart_num = config->port_num;

    const uart_config_t uart_cfg = {
        .baud_rate = 9600,
        .data_bits = UART_DATA_8_BITS,
        .parity    = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    esp_err_t err = uart_driver_install(s_uart_num, JSN_UART_BUF_SIZE, 0, 0, NULL, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to install UART driver: %s", esp_err_to_name(err));
        return err;
    }

    err = uart_param_config(s_uart_num, &uart_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to configure UART: %s", esp_err_to_name(err));
        uart_driver_delete(s_uart_num);
        return err;
    }

    err = uart_set_pin(s_uart_num, config->pin_a, config->pin_b, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set UART pins: %s", esp_err_to_name(err));
        uart_driver_delete(s_uart_num);
        return err;
    }

    ESP_LOGI(TAG, "Initialized on UART%d (TX=%d, RX=%d)", s_uart_num, config->pin_a, config->pin_b);
    return ESP_OK;
}

esp_err_t jsn_sr04t_read_distance_mm(float *distance_mm)
{
    if (s_uart_num < 0) {
        return ESP_ERR_INVALID_STATE;
    }

    for (int retry = 0; retry < JSN_MAX_RETRIES; retry++) {
        // Flush any stale data
        uart_flush_input(s_uart_num);

        // Send trigger byte
        const uint8_t trigger = JSN_TRIGGER_BYTE;
        uart_write_bytes(s_uart_num, &trigger, 1);

        // Read response: [0xFF, HIGH, LOW, CHECKSUM]
        uint8_t buf[JSN_RESPONSE_LEN];
        int len = uart_read_bytes(s_uart_num, buf, JSN_RESPONSE_LEN,
                                  pdMS_TO_TICKS(JSN_READ_TIMEOUT_MS));

        if (len != JSN_RESPONSE_LEN) {
            ESP_LOGW(TAG, "Read timeout (got %d bytes, attempt %d/%d)", len, retry + 1, JSN_MAX_RETRIES);
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        if (buf[0] != JSN_HEADER_BYTE) {
            ESP_LOGW(TAG, "Invalid header: 0x%02X (attempt %d/%d)", buf[0], retry + 1, JSN_MAX_RETRIES);
            continue;
        }

        uint8_t checksum = (buf[0] + buf[1] + buf[2]) & 0xFF;
        if (checksum != buf[3]) {
            ESP_LOGW(TAG, "Checksum mismatch: calc=0x%02X recv=0x%02X (attempt %d/%d)",
                     checksum, buf[3], retry + 1, JSN_MAX_RETRIES);
            continue;
        }

        uint16_t raw_mm = ((uint16_t)buf[1] << 8) | buf[2];

        if (raw_mm < JSN_MIN_RANGE_MM || raw_mm > JSN_MAX_RANGE_MM) {
            ESP_LOGW(TAG, "Out of range: %u mm (attempt %d/%d)", raw_mm, retry + 1, JSN_MAX_RETRIES);
            continue;
        }

        *distance_mm = (float)raw_mm;
        ESP_LOGD(TAG, "Distance: %.0f mm", *distance_mm);
        return ESP_OK;
    }

    ESP_LOGE(TAG, "Failed to read after %d retries", JSN_MAX_RETRIES);
    return ESP_ERR_TIMEOUT;
}

void jsn_sr04t_deinit(void)
{
    if (s_uart_num >= 0) {
        uart_driver_delete(s_uart_num);
        s_uart_num = -1;
        ESP_LOGI(TAG, "Deinitialized");
    }
}

const sensor_driver_t jsn_sr04t_driver = {
    .name = "JSN-SR04T",
    .init = jsn_sr04t_init,
    .read_distance_mm = jsn_sr04t_read_distance_mm,
    .deinit = jsn_sr04t_deinit,
};
