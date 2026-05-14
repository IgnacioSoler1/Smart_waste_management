#include "sim800l.h"

#include <string.h>
#include <stdio.h>
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_modem_api.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

static const char *TAG = "sim800l";

#define SIM_UART_BUF_SIZE    2048
#define SIM_AT_TIMEOUT_MS    3000
#define SIM_PWRKEY_PULSE_MS  1200
#define SIM_BOOT_WAIT_MS     3000
#define SIM_GPRS_TIMEOUT_MS  30000

// Event bits for PPP connection state
#define PPP_CONNECTED_BIT    BIT0
#define PPP_DISCONNECTED_BIT BIT1

static esp_modem_dce_t *s_dce = NULL;
static esp_netif_t *s_ppp_netif = NULL;
static EventGroupHandle_t s_event_group = NULL;
static sim800l_config_t s_config;
static bool s_powered_on = false;

static void on_ppp_changed(void *arg, esp_event_base_t base, int32_t event_id, void *data)
{
    if (base == IP_EVENT && event_id == IP_EVENT_PPP_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "PPP got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_event_group, PPP_CONNECTED_BIT);
    } else if (base == IP_EVENT && event_id == IP_EVENT_PPP_LOST_IP) {
        ESP_LOGW(TAG, "PPP lost IP");
        xEventGroupSetBits(s_event_group, PPP_DISCONNECTED_BIT);
    }
}

esp_err_t sim800l_power_on(const sim800l_config_t *config)
{
    memcpy(&s_config, config, sizeof(sim800l_config_t));

    // Configure PWRKEY pin
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << config->pwrkey_pin),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);

    // Pulse PWRKEY to power on (active low, hold > 1s)
    ESP_LOGI(TAG, "Powering on (PWRKEY pulse on GPIO%d)", config->pwrkey_pin);
    gpio_set_level(config->pwrkey_pin, 1);
    vTaskDelay(pdMS_TO_TICKS(100));
    gpio_set_level(config->pwrkey_pin, 0);
    vTaskDelay(pdMS_TO_TICKS(SIM_PWRKEY_PULSE_MS));
    gpio_set_level(config->pwrkey_pin, 1);

    // Wait for module to boot
    vTaskDelay(pdMS_TO_TICKS(SIM_BOOT_WAIT_MS));
    s_powered_on = true;

    ESP_LOGI(TAG, "Power on complete");
    return ESP_OK;
}

esp_err_t sim800l_init(const sim800l_config_t *config)
{
    if (s_event_group == NULL) {
        s_event_group = xEventGroupCreate();
    }

    // Initialize network interface and event loop (idempotent)
    esp_netif_init();
    esp_event_loop_create_default();

    // Register PPP IP events
    esp_event_handler_register(IP_EVENT, IP_EVENT_PPP_GOT_IP, &on_ppp_changed, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_PPP_LOST_IP, &on_ppp_changed, NULL);

    // Create PPP network interface
    esp_netif_config_t netif_ppp_config = ESP_NETIF_DEFAULT_PPP();
    s_ppp_netif = esp_netif_new(&netif_ppp_config);

    // Configure DTE (UART to modem)
    esp_modem_dte_config_t dte_config = ESP_MODEM_DTE_DEFAULT_CONFIG();
    dte_config.uart_config.port_num = config->uart_num;
    dte_config.uart_config.tx_io_num = config->tx_pin;
    dte_config.uart_config.rx_io_num = config->rx_pin;
    dte_config.uart_config.baud_rate = config->baudrate;
    dte_config.uart_config.rx_buffer_size = SIM_UART_BUF_SIZE;
    dte_config.uart_config.tx_buffer_size = SIM_UART_BUF_SIZE;
    dte_config.task_stack_size = 4096;

    // Configure DCE (modem type)
    esp_modem_dce_config_t dce_config = ESP_MODEM_DCE_DEFAULT_CONFIG(config->apn);

    s_dce = esp_modem_new_dev(ESP_MODEM_DCE_SIM800, &dte_config, &dce_config, s_ppp_netif);
    if (s_dce == NULL) {
        ESP_LOGE(TAG, "Failed to create modem DCE");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Initialized modem on UART%d (TX=%d, RX=%d, baud=%d, APN=%s)",
             config->uart_num, config->tx_pin, config->rx_pin, config->baudrate, config->apn);
    return ESP_OK;
}

esp_err_t sim800l_connect_ppp(void)
{
    if (s_dce == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(TAG, "Switching to PPP data mode...");
    xEventGroupClearBits(s_event_group, PPP_CONNECTED_BIT | PPP_DISCONNECTED_BIT);

    esp_err_t err = esp_modem_set_mode(s_dce, ESP_MODEM_MODE_DATA);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to enter data mode: %s", esp_err_to_name(err));
        return err;
    }

    // Wait for PPP to get an IP address
    EventBits_t bits = xEventGroupWaitBits(s_event_group,
                                           PPP_CONNECTED_BIT | PPP_DISCONNECTED_BIT,
                                           pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(SIM_GPRS_TIMEOUT_MS));

    if (!(bits & PPP_CONNECTED_BIT)) {
        ESP_LOGE(TAG, "PPP connection timeout");
        return ESP_ERR_TIMEOUT;
    }

    ESP_LOGI(TAG, "PPP connected");
    return ESP_OK;
}

esp_err_t sim800l_disconnect(void)
{
    if (s_dce == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(TAG, "Disconnecting PPP...");
    esp_err_t err = esp_modem_set_mode(s_dce, ESP_MODEM_MODE_COMMAND);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Failed to switch to command mode: %s", esp_err_to_name(err));
    }

    return ESP_OK;
}

void sim800l_power_off(void)
{
    if (s_dce != NULL) {
        esp_modem_destroy(s_dce);
        s_dce = NULL;
    }

    if (s_ppp_netif != NULL) {
        esp_netif_destroy(s_ppp_netif);
        s_ppp_netif = NULL;
    }

    if (s_powered_on) {
        // Pulse PWRKEY to power off
        gpio_set_level(s_config.pwrkey_pin, 0);
        vTaskDelay(pdMS_TO_TICKS(SIM_PWRKEY_PULSE_MS));
        gpio_set_level(s_config.pwrkey_pin, 1);
        s_powered_on = false;
        ESP_LOGI(TAG, "Powered off");
    }
}

bool sim800l_is_connected(void)
{
    if (s_event_group == NULL) return false;
    EventBits_t bits = xEventGroupGetBits(s_event_group);
    return (bits & PPP_CONNECTED_BIT) != 0;
}
