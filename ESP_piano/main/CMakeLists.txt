cmake_minimum_required(VERSION 3.16)

set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_SOURCE_DIR}/../../contracts")

idf_component_register(
    SRCS
        "main.cpp"
        "adc_sampler.c"
        "audio_dsp.c"
        "network.cpp"
        "web_server.cpp"
    INCLUDE_DIRS "." "core" "hal"
    PRIV_REQUIRES
        esp_adc esp_timer esp_driver_gpio esp_wifi nvs_flash esp_http_server
)
