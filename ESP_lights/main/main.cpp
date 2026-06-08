#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_wifi.h"
#include "esp_mac.h"
#include "esp_log.h"
#include "nvs_flash.h"

static const char *TAG = "MAC_FINDER";

extern "C" void app_main(void) {
    // 1. Initialize NVS (Required for Wi-Fi/MAC operations)
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    // 2. Put the Wi-Fi hardware into Station Mode
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_start();

    // 3. Retrieve and format the MAC Address
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);

    // 4. Print it beautifully to the terminal
    ESP_LOGI(TAG, "=======================================");
    ESP_LOGI(TAG, "RECEIVER MAC ADDRESS: %02X:%02X:%02X:%02X:%02X:%02X", 
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    ESP_LOGI(TAG, "=======================================");
}