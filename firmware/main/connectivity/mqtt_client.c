#include "mqtt_client.h"

#include <string.h>
#include "esp_log.h"
#include "mqtt_client.h"
// ESP-IDF MQTT library header (different from our header)
#include "esp_mqtt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

// Rename to avoid conflict with ESP-IDF's mqtt_client.h
// We use the esp_mqtt_client_* API from ESP-IDF's mqtt component.

static const char *TAG = "mqtt";

#define MQTT_CONNECTED_BIT    BIT0
#define MQTT_PUBLISHED_BIT    BIT1
#define MQTT_ERROR_BIT        BIT2
#define MQTT_CONNECT_TIMEOUT  15000
#define MQTT_PUBLISH_TIMEOUT  10000

static esp_mqtt_client_handle_t s_client = NULL;
static EventGroupHandle_t s_mqtt_events = NULL;

static void mqtt_event_handler(void *arg, esp_event_base_t base, int32_t event_id, void *data)
{
    esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)data;

    switch (event->event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "Connected to broker");
        xEventGroupSetBits(s_mqtt_events, MQTT_CONNECTED_BIT);
        break;

    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "Disconnected from broker");
        xEventGroupClearBits(s_mqtt_events, MQTT_CONNECTED_BIT);
        break;

    case MQTT_EVENT_PUBLISHED:
        ESP_LOGI(TAG, "Message published (msg_id=%d)", event->msg_id);
        xEventGroupSetBits(s_mqtt_events, MQTT_PUBLISHED_BIT);
        break;

    case MQTT_EVENT_ERROR:
        ESP_LOGE(TAG, "MQTT error type: %d", event->error_handle->error_type);
        if (event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
            ESP_LOGE(TAG, "TLS error: 0x%04x, transport errno: %d",
                     event->error_handle->esp_tls_last_esp_err,
                     event->error_handle->esp_transport_sock_errno);
        }
        xEventGroupSetBits(s_mqtt_events, MQTT_ERROR_BIT);
        break;

    default:
        break;
    }
}

esp_err_t mqtt_client_connect(const mqtt_client_config_t *config)
{
    if (s_mqtt_events == NULL) {
        s_mqtt_events = xEventGroupCreate();
    }

    xEventGroupClearBits(s_mqtt_events, MQTT_CONNECTED_BIT | MQTT_ERROR_BIT);

    const esp_mqtt_client_config_t mqtt_cfg = {
        .broker = {
            .address.uri = config->broker_uri,
            .verification.certificate = config->root_ca_pem,
        },
        .credentials = {
            .client_id = config->client_id,
            .authentication = {
                .certificate = config->device_cert_pem,
                .key = config->device_key_pem,
            },
        },
        .network.timeout_ms = 10000,
        .session.keepalive = 30,
    };

    s_client = esp_mqtt_client_init(&mqtt_cfg);
    if (s_client == NULL) {
        ESP_LOGE(TAG, "Failed to init MQTT client");
        return ESP_FAIL;
    }

    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);

    esp_err_t err = esp_mqtt_client_start(s_client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start MQTT client: %s", esp_err_to_name(err));
        esp_mqtt_client_destroy(s_client);
        s_client = NULL;
        return err;
    }

    // Wait for connection
    EventBits_t bits = xEventGroupWaitBits(s_mqtt_events,
                                           MQTT_CONNECTED_BIT | MQTT_ERROR_BIT,
                                           pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(MQTT_CONNECT_TIMEOUT));

    if (bits & MQTT_ERROR_BIT) {
        ESP_LOGE(TAG, "Connection failed");
        esp_mqtt_client_destroy(s_client);
        s_client = NULL;
        return ESP_FAIL;
    }

    if (!(bits & MQTT_CONNECTED_BIT)) {
        ESP_LOGE(TAG, "Connection timeout");
        esp_mqtt_client_destroy(s_client);
        s_client = NULL;
        return ESP_ERR_TIMEOUT;
    }

    return ESP_OK;
}

esp_err_t mqtt_client_publish(const char *topic, const char *payload, int qos)
{
    if (s_client == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    xEventGroupClearBits(s_mqtt_events, MQTT_PUBLISHED_BIT);

    int msg_id = esp_mqtt_client_publish(s_client, topic, payload, strlen(payload), qos, 0);
    if (msg_id < 0) {
        ESP_LOGE(TAG, "Publish failed");
        return ESP_FAIL;
    }

    if (qos > 0) {
        // Wait for PUBACK
        EventBits_t bits = xEventGroupWaitBits(s_mqtt_events,
                                               MQTT_PUBLISHED_BIT | MQTT_ERROR_BIT,
                                               pdFALSE, pdFALSE,
                                               pdMS_TO_TICKS(MQTT_PUBLISH_TIMEOUT));
        if (!(bits & MQTT_PUBLISHED_BIT)) {
            ESP_LOGE(TAG, "Publish timeout (msg_id=%d)", msg_id);
            return ESP_ERR_TIMEOUT;
        }
    }

    ESP_LOGI(TAG, "Published to %s (msg_id=%d, qos=%d)", topic, msg_id, qos);
    return ESP_OK;
}

void mqtt_client_disconnect(void)
{
    if (s_client != NULL) {
        esp_mqtt_client_disconnect(s_client);
        esp_mqtt_client_stop(s_client);
        esp_mqtt_client_destroy(s_client);
        s_client = NULL;
        ESP_LOGI(TAG, "Disconnected");
    }
}
