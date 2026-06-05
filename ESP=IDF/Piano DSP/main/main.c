#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "soc/soc_caps.h"

/* Link the newly created modules */
#include "adc_sampler.h"
#include "audio_dsp.h"

#define ONBOARD_LED_GPIO 2

static const char *TAG = "AUDIO_MAIN";
static float sample_buffer[FFT_SIZE];
static int   sample_count = 0;

void blink_heartbeat_task(void *pvParameters)
{
    gpio_reset_pin(ONBOARD_LED_GPIO);
    gpio_set_direction(ONBOARD_LED_GPIO, GPIO_MODE_OUTPUT);
    while (1) {
        gpio_set_level(ONBOARD_LED_GPIO, 1);
        vTaskDelay(pdMS_TO_TICKS(1000));
        gpio_set_level(ONBOARD_LED_GPIO, 0);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void app_main(void)
{
    xTaskCreate(blink_heartbeat_task, "blink_task", 2048, NULL, 5, NULL);
    
    // Initialize our abstract modules
    init_dsp_library();
    
    adc_continuous_handle_t adc_handle = NULL;  
    init_continuous_adc(&adc_handle);
    ESP_ERROR_CHECK(adc_continuous_start(adc_handle));

    static uint8_t raw_result_buffer[READ_LENGTH] = {0};
    uint32_t bytes_read = 0;

    ESP_LOGI(TAG, "=== Sampling started, FFT size = %d, resolution = %.1f Hz ===",
             FFT_SIZE, (float)SAMPLE_RATE / FFT_SIZE);

    while (1) {
        esp_err_t ret = adc_continuous_read(adc_handle, raw_result_buffer,      
                                            READ_LENGTH, &bytes_read, portMAX_DELAY);

        if (ret == ESP_OK) {        
            for (int i = 0; i < bytes_read; i += SOC_ADC_DIGI_RESULT_BYTES) {   
                if (sample_count >= FFT_SIZE) break;
                
                adc_digi_output_data_t *output_data_pointer = (adc_digi_output_data_t*)&raw_result_buffer[i];
                sample_buffer[sample_count++] = (float)output_data_pointer->type1.data;
            }

            if (sample_count >= FFT_SIZE) {
                float avg_adc_val = 0.0f;
                
                // 1. Get amplitude using our ADC module
                uint32_t signal_amplitude_mv = get_peak_to_peak_mv(sample_buffer, FFT_SIZE, &avg_adc_val);

                // 2. Process the frame using our DSP module
                process_audio_frame(sample_buffer, signal_amplitude_mv);
                
                sample_count = 0;
            }
        } else {    
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    }
}