#pragma once

// SmartWaste MVD — Application configuration constants
// Values come from Kconfig (menuconfig) with sensible defaults.

// ── Container ──────────────────────────────────────────
#define APP_CONTAINER_ID          CONFIG_SMARTWASTE_CONTAINER_ID
#define APP_CONTAINER_HEIGHT_MM   CONFIG_SMARTWASTE_CONTAINER_HEIGHT_MM
#define APP_LATITUDE              CONFIG_SMARTWASTE_LATITUDE
#define APP_LONGITUDE             CONFIG_SMARTWASTE_LONGITUDE

// ── Sensors ────────────────────────────────────────────
#define APP_JSN_UART_NUM          CONFIG_SMARTWASTE_JSN_SR04T_UART_NUM
#define APP_JSN_TX_PIN            CONFIG_SMARTWASTE_JSN_SR04T_TX_PIN
#define APP_JSN_RX_PIN            CONFIG_SMARTWASTE_JSN_SR04T_RX_PIN
#define APP_JSN_MOUNT_ANGLE_DEG   CONFIG_SMARTWASTE_JSN_SR04T_MOUNT_ANGLE_DEG

#define APP_VL53_SDA_PIN          CONFIG_SMARTWASTE_VL53L1X_SDA_PIN
#define APP_VL53_SCL_PIN          CONFIG_SMARTWASTE_VL53L1X_SCL_PIN
#define APP_VL53_I2C_NUM          CONFIG_SMARTWASTE_VL53L1X_I2C_NUM
#define APP_VL53_MOUNT_ANGLE_DEG  CONFIG_SMARTWASTE_VL53L1X_MOUNT_ANGLE_DEG

#define APP_SENSOR_READINGS       CONFIG_SMARTWASTE_SENSOR_READINGS_COUNT

// ── WiFi (dev mode) ─────────────────────────────────────
#ifdef CONFIG_SMARTWASTE_CONNECTIVITY_WIFI
#define APP_WIFI_SSID             CONFIG_SMARTWASTE_WIFI_SSID
#define APP_WIFI_PASSWORD         CONFIG_SMARTWASTE_WIFI_PASSWORD
#endif

// ── SIM800L ────────────────────────────────────────────
#define APP_SIM_UART_NUM          CONFIG_SMARTWASTE_SIM800L_UART_NUM
#define APP_SIM_TX_PIN            CONFIG_SMARTWASTE_SIM800L_TX_PIN
#define APP_SIM_RX_PIN            CONFIG_SMARTWASTE_SIM800L_RX_PIN
#define APP_SIM_PWRKEY_PIN        CONFIG_SMARTWASTE_SIM800L_PWRKEY_PIN
#define APP_SIM_BAUDRATE          CONFIG_SMARTWASTE_SIM800L_BAUDRATE
#define APP_APN                   CONFIG_SMARTWASTE_APN

// ── MQTT ───────────────────────────────────────────────
#define APP_MQTT_BROKER_URI       CONFIG_SMARTWASTE_MQTT_BROKER_URI
#define APP_MQTT_TOPIC_PREFIX     CONFIG_SMARTWASTE_MQTT_TOPIC_PREFIX
#define APP_MQTT_QOS              CONFIG_SMARTWASTE_MQTT_QOS

// ── Power ──────────────────────────────────────────────
#define APP_DEEP_SLEEP_SEC        CONFIG_SMARTWASTE_DEEP_SLEEP_SECONDS
#define APP_MAX_AWAKE_SEC         CONFIG_SMARTWASTE_MAX_AWAKE_SECONDS

// ── Battery ────────────────────────────────────────────
#define APP_BATTERY_ADC_CHANNEL   CONFIG_SMARTWASTE_BATTERY_ADC_CHANNEL
#define APP_BATTERY_DIVIDER_RATIO (CONFIG_SMARTWASTE_BATTERY_DIVIDER_RATIO / 100.0f)

// ── NVS Keys ───────────────────────────────────────────
#define NVS_NAMESPACE             "smartwaste"
#define NVS_KEY_DEVICE_CERT       "dev_cert"
#define NVS_KEY_DEVICE_KEY        "dev_key"
#define NVS_KEY_ROOT_CA           "root_ca"
#define NVS_KEY_CONTAINER_ID      "container_id"

// ── NTP ────────────────────────────────────────────────
#define APP_NTP_SERVER_PRIMARY    "pool.ntp.org"
#define APP_NTP_SERVER_SECONDARY  "time.google.com"
#define APP_NTP_MAX_DRIFT_SEC     30

// ── Misc ───────────────────────────────────────────────
#define APP_MQTT_TOPIC_MAX_LEN    128
#define APP_JSON_PAYLOAD_MAX_LEN  512
