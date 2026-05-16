// SmartWaste MVD — ESP32 Sensor Firmware
// Entry point: wake → measure → publish → sleep
//
// Full cycle:
//   1. Read boot count from RTC memory
//   2. Read config from NVS (container_id, certs)
//   3. Init sensors (JSN-SR04T + VL53L1X)
//   4. Take measurements + filter outliers
//   5. Calculate fill_level
//   6. Read battery voltage (ADC)
//   7. Read temperature (internal sensor)
//   8. Power on SIM800L + connect GPRS (PPP)
//   9. Sync NTP if needed
//  10. Connect MQTT (TLS mutual auth)
//  11. Publish JSON to smartwaste-dev/sensors/{container_id}
//  12. Cleanup: disconnect, power off modem, deinit sensors
//  13. Deep sleep 15 min

#include <stdio.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>
#include <math.h>

#include "esp_log.h"
#include "esp_random.h"
#ifndef CONFIG_SMARTWASTE_SIMULATE_SENSORS
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#endif
#include "nvs_flash.h"
#include "nvs.h"

#include "app_config.h"
#ifndef CONFIG_SMARTWASTE_SIMULATE_SENSORS
#include "sensors/sensor_interface.h"
#include "sensors/jsn_sr04t.h"
#include "sensors/vl53l1x.h"
#include "measurement/outlier_filter.h"
#include "measurement/fill_calculator.h"
#endif

#ifdef CONFIG_SMARTWASTE_CONNECTIVITY_WIFI
#include "connectivity/wifi_sta.h"
#else
#include "connectivity/sim800l.h"
#endif

#include "connectivity/sw_mqtt_client.h"
#include "connectivity/ntp_sync.h"
#include "power/sleep_manager.h"

static const char *TAG = "main";

// Amazon Root CA 1 (ATS) — embedded for TLS to AWS IoT Core
// In production, this would be read from NVS along with device certs.
// This is the public Amazon Root CA, not a secret.
extern const uint8_t aws_root_ca_pem_start[] asm("_binary_AmazonRootCA1_pem_start");
extern const uint8_t aws_root_ca_pem_end[]   asm("_binary_AmazonRootCA1_pem_end");

#ifndef CONFIG_SMARTWASTE_SIMULATE_SENSORS
// ── Battery reading ────────────────────────────────────

static float read_battery_percent(void)
{
    adc_oneshot_unit_handle_t adc_handle;
    adc_oneshot_unit_init_cfg_t init_cfg = {
        .unit_id = ADC_UNIT_1,
    };

    if (adc_oneshot_new_unit(&init_cfg, &adc_handle) != ESP_OK) {
        ESP_LOGW(TAG, "ADC init failed, returning 0");
        return 0.0f;
    }

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    adc_oneshot_config_channel(adc_handle, APP_BATTERY_ADC_CHANNEL, &chan_cfg);

    int raw = 0;
    adc_oneshot_read(adc_handle, APP_BATTERY_ADC_CHANNEL, &raw);
    adc_oneshot_del_unit(adc_handle);

    // Convert raw ADC to voltage (12-bit, 0-3.3V with 12dB attenuation)
    float voltage = (raw / 4095.0f) * 3.3f * APP_BATTERY_DIVIDER_RATIO;

    // Simple linear mapping: 3.0V = 0%, 4.2V = 100% (LiPo)
    float percent = (voltage - 3.0f) / (4.2f - 3.0f) * 100.0f;
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;

    ESP_LOGI(TAG, "Battery: %.2fV → %.1f%%", voltage, percent);
    return percent;
}

// ── Temperature reading ────────────────────────────────

static float read_temperature(void)
{
    // ESP32 internal temperature sensor
    // Note: accuracy is limited (~+/-5°C), but useful for diagnostics
    // For accurate ambient temp, use an external sensor (e.g., DS18B20)
    // For now, return a placeholder — internal temp sensor API varies by ESP-IDF version
    return 25.0f; // TODO: implement with temperature_sensor_install() on ESP-IDF 5.x
}
#endif

// ── NVS helpers ────────────────────────────────────────

static esp_err_t nvs_read_string(nvs_handle_t handle, const char *key, char *buf, size_t buf_size)
{
    size_t len = buf_size;
    return nvs_get_str(handle, key, buf, &len);
}

// ── Main ───────────────────────────────────────────────

void app_main(void)
{
    // Step 1: Read boot count from RTC memory
    rtc_state_t *rtc = sleep_manager_get_state();
    rtc->boot_count++;
    ESP_LOGI(TAG, "=== SmartWaste Sensor Boot #%lu ===", (unsigned long)rtc->boot_count);

    // Start safety watchdog (force sleep if we're awake too long)
    sleep_manager_start_watchdog(APP_MAX_AWAKE_SEC);

    // Step 2: Init NVS and read configuration
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "NVS init failed: %s", esp_err_to_name(err));
        sleep_manager_enter_deep_sleep(APP_DEEP_SLEEP_SEC);
    }

    // Read container_id from NVS (falls back to Kconfig default)
    char container_id[32] = APP_CONTAINER_ID;
    nvs_handle_t nvs_handle;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs_handle) == ESP_OK) {
        nvs_read_string(nvs_handle, NVS_KEY_CONTAINER_ID, container_id, sizeof(container_id));
        nvs_close(nvs_handle);
    }
    ESP_LOGI(TAG, "Container ID: %s", container_id);

    // Read device certificates from NVS
    char *device_cert = NULL;
    char *device_key = NULL;
    size_t cert_len = 0, key_len = 0;

    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs_handle) == ESP_OK) {
        // Get sizes first
        if (nvs_get_str(nvs_handle, NVS_KEY_DEVICE_CERT, NULL, &cert_len) == ESP_OK) {
            device_cert = malloc(cert_len);
            nvs_get_str(nvs_handle, NVS_KEY_DEVICE_CERT, device_cert, &cert_len);
        }
        if (nvs_get_str(nvs_handle, NVS_KEY_DEVICE_KEY, NULL, &key_len) == ESP_OK) {
            device_key = malloc(key_len);
            nvs_get_str(nvs_handle, NVS_KEY_DEVICE_KEY, device_key, &key_len);
        }
        nvs_close(nvs_handle);
    }

    if (device_cert == NULL || device_key == NULL) {
        ESP_LOGE(TAG, "Device certificate or key not found in NVS!");
        ESP_LOGE(TAG, "Flash certificates using: idf.py -p /dev/ttyUSB0 nvs-flash");
        free(device_cert);
        free(device_key);
        sleep_manager_enter_deep_sleep(APP_DEEP_SLEEP_SEC);
    }

    // Steps 3-7: Sensors — real or simulated
#ifdef CONFIG_SMARTWASTE_SIMULATE_SENSORS
    float fill_level = (float)(esp_random() % 1000) / 10.0f; // 0.0-99.9%
    float battery = 85.0f;
    float temperature = 22.0f;
    ESP_LOGI(TAG, "[SIMULATED] fill=%.1f%% bat=%.1f%% temp=%.1f°C", fill_level, battery, temperature);
#else
    // Step 3: Init sensors
    sensor_config_t jsn_config = {
        .pin_a = APP_JSN_TX_PIN,
        .pin_b = APP_JSN_RX_PIN,
        .port_num = APP_JSN_UART_NUM,
        .angle_deg = APP_JSN_MOUNT_ANGLE_DEG,
    };

    sensor_config_t vl53_config = {
        .pin_a = APP_VL53_SDA_PIN,
        .pin_b = APP_VL53_SCL_PIN,
        .port_num = APP_VL53_I2C_NUM,
        .angle_deg = APP_VL53_MOUNT_ANGLE_DEG,
    };

    bool jsn_ok = (jsn_sr04t_driver.init(&jsn_config) == ESP_OK);
    bool vl53_ok = (vl53l1x_driver.init(&vl53_config) == ESP_OK);

    if (!jsn_ok && !vl53_ok) {
        ESP_LOGE(TAG, "No sensors available! Sleeping...");
        free(device_cert);
        free(device_key);
        sleep_manager_enter_deep_sleep(APP_DEEP_SLEEP_SEC);
    }

    // Step 4: Take measurements + filter outliers
    fill_sensor_reading_t readings[2];
    int reading_count = 0;

    if (jsn_ok) {
        float median = 0;
        if (outlier_filter_read_median(&jsn_sr04t_driver, APP_SENSOR_READINGS, &median) == ESP_OK) {
            readings[reading_count++] = (fill_sensor_reading_t){
                .distance_mm = median,
                .angle_deg = APP_JSN_MOUNT_ANGLE_DEG,
                .weight = 1.0f,
            };
        }
    }

    if (vl53_ok) {
        float median = 0;
        if (outlier_filter_read_median(&vl53l1x_driver, APP_SENSOR_READINGS, &median) == ESP_OK) {
            readings[reading_count++] = (fill_sensor_reading_t){
                .distance_mm = median,
                .angle_deg = APP_VL53_MOUNT_ANGLE_DEG,
                .weight = 0.7f,
            };
        }
    }

    // Step 5: Calculate fill level
    float fill_level = 0;
    if (reading_count > 0) {
        err = fill_calculator_compute(APP_CONTAINER_HEIGHT_MM, readings, reading_count, &fill_level);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Fill calculation failed, using last known: %.1f%%", rtc->last_fill_level);
            fill_level = rtc->last_fill_level >= 0 ? rtc->last_fill_level : 0;
        }
    } else {
        ESP_LOGW(TAG, "No valid readings, using last known: %.1f%%", rtc->last_fill_level);
        fill_level = rtc->last_fill_level >= 0 ? rtc->last_fill_level : 0;
    }
    rtc->last_fill_level = fill_level;

    // Step 6: Read battery
    float battery = read_battery_percent();

    // Step 7: Read temperature
    float temperature = read_temperature();
#endif

    // Step 8: Network connectivity — WiFi or GPRS
#ifdef CONFIG_SMARTWASTE_CONNECTIVITY_WIFI
    err = sw_wifi_init(APP_WIFI_SSID, APP_WIFI_PASSWORD);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WiFi connection failed");
        goto cleanup;
    }
#else
    sim800l_config_t sim_config = {
        .uart_num = APP_SIM_UART_NUM,
        .tx_pin = APP_SIM_TX_PIN,
        .rx_pin = APP_SIM_RX_PIN,
        .pwrkey_pin = APP_SIM_PWRKEY_PIN,
        .baudrate = APP_SIM_BAUDRATE,
        .apn = APP_APN,
    };

    err = sim800l_power_on(&sim_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SIM800L power on failed");
        goto cleanup;
    }

    err = sim800l_init(&sim_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SIM800L init failed");
        goto cleanup;
    }

    err = sim800l_connect_ppp();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "GPRS connection failed");
        goto cleanup;
    }
#endif

    // Step 9: Sync NTP if needed
    ntp_sync_init();
    ntp_sync_time(APP_NTP_MAX_DRIFT_SEC);

    // Step 10: Connect MQTT
    char client_id[64];
    snprintf(client_id, sizeof(client_id), "%s-%s", APP_MQTT_TOPIC_PREFIX, container_id);

    mqtt_client_config_t mqtt_config = {
        .broker_uri = APP_MQTT_BROKER_URI,
        .client_id = client_id,
        .device_cert_pem = device_cert,
        .device_key_pem = device_key,
        .root_ca_pem = (const char *)aws_root_ca_pem_start,
    };

    err = mqtt_client_connect(&mqtt_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "MQTT connection failed");
        goto cleanup;
    }

    // Step 11: Publish sensor reading
    // Build topic: smartwaste-dev/sensors/{container_id}
    char topic[APP_MQTT_TOPIC_MAX_LEN];
    snprintf(topic, sizeof(topic), "%s/sensors/%s", APP_MQTT_TOPIC_PREFIX, container_id);

    // Build timestamp
    time_t now;
    time(&now);
    struct tm timeinfo;
    gmtime_r(&now, &timeinfo);
    char timestamp[32];
    strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%S+00:00", &timeinfo);

    // Build JSON payload (matches simulator format exactly)
    char payload[APP_JSON_PAYLOAD_MAX_LEN];
    snprintf(payload, sizeof(payload),
        "{"
            "\"container_id\":\"%s\","
            "\"timestamp\":\"%s\","
            "\"fill_level\":%.1f,"
            "\"battery\":%.1f,"
            "\"temperature\":%.1f,"
            "\"latitude\":%s,"
            "\"longitude\":%s"
        "}",
        container_id,
        timestamp,
        fill_level,
        battery,
        temperature,
        APP_LATITUDE,
        APP_LONGITUDE
    );

    ESP_LOGI(TAG, "Publishing to %s", topic);
    ESP_LOGI(TAG, "Payload: %s", payload);

    err = mqtt_client_publish(topic, payload, APP_MQTT_QOS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "MQTT publish failed");
    } else {
        ESP_LOGI(TAG, "Published successfully");
    }

cleanup:
    // Step 12: Cleanup
    mqtt_client_disconnect();
    ntp_sync_deinit();

#ifdef CONFIG_SMARTWASTE_CONNECTIVITY_WIFI
    sw_wifi_disconnect();
#else
    sim800l_disconnect();
    sim800l_power_off();
#endif

#ifndef CONFIG_SMARTWASTE_SIMULATE_SENSORS
    if (jsn_ok) jsn_sr04t_driver.deinit();
    if (vl53_ok) vl53l1x_driver.deinit();
#endif

    free(device_cert);
    free(device_key);

    // Step 13: Deep sleep
    ESP_LOGI(TAG, "Cycle complete. Sleeping for %d seconds...", APP_DEEP_SLEEP_SEC);
    sleep_manager_enter_deep_sleep(APP_DEEP_SLEEP_SEC);
}
