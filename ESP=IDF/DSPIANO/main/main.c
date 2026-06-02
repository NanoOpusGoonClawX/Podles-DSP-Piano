#include <stdio.h>
#include <stdbool.h>
#include <math.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_adc/adc_continuous.h"
#include "soc/soc_caps.h"

#include "esp_dsp.h"
#include "dsps_fft2r.h"
#include "dsps_math.h"

/* ============================================================
 * CONSTANTS
 * ============================================================ */

#define ONBOARD_LED_GPIO             2        // Onboard blue LED on classic ESP32 DevKitC
#define SAMPLE_RATE                  20480    // Raised above hardware minimum of 20kHz
#define READ_LENGTH                  1024     
#define FFT_SIZE                     2048     // Doubled to maintain 10Hz frequency resolution
#define HPS_HARMONICS                5        

#define HOLD_WINDOW_MS               200      
#define MIN_VALID_MAGNITUDE          800.0f   
#define MIN_PEAK_TO_AVERAGE_RATIO    5.0f     
#define NOTE_ACTIVATION_THRESHOLD_MV 150      
#define NOTE_DEBOUNCE_FRAMES         3        

static const char *TAG = "AUDIO_DEBUG";

/* ============================================================
 * STATE
 * ============================================================ */

__attribute__((aligned(16))) static float fft_input[FFT_SIZE * 2];   
static float fft_window[FFT_SIZE];      
static float fft_magnitude[FFT_SIZE / 2]; 
static float sample_buffer[FFT_SIZE];
static int   sample_count = 0;

static float   hold_peak_magnitude = 0.0f;
static int     hold_peak_bin_index = -1;
static float   hold_peak_signal_to_noise_ratio = 0.0f;
static int64_t hold_start_time = 0;

static int last_candidate_midi    = -1;
static int candidate_repeat_count = 0;

static float dc_bias = 2048.0f;         

/* ============================================================
 * HELPERS
 * ============================================================ */

static inline float remove_dc_offset(float sample)
{
    dc_bias = 0.999f * dc_bias + 0.001f * sample;
    return sample - dc_bias;
}

uint32_t get_peak_to_peak_mv(const float* raw_samples, uint32_t buffer_size, float* out_avg) 
{
    if (raw_samples == NULL || buffer_size == 0) return 0;

    float max_sample = 0.0f;
    float min_sample = 4095.0f; 
    float sum_sample = 0.0f;

    for (uint32_t i = 0; i < buffer_size; i++) {
        if (raw_samples[i] > max_sample) max_sample = raw_samples[i];
        if (raw_samples[i] < min_sample) min_sample = raw_samples[i];
        sum_sample += raw_samples[i];
    }

    if (out_avg != NULL) {
        *out_avg = sum_sample / buffer_size;
    }

    float peak_to_peak_adc = max_sample - min_sample;
    return (uint32_t)((peak_to_peak_adc * 3300.0f) / 4095.0f);
}

static const char *note_names[12] = {
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B"
};

static void frequency_to_note(float frequency_hz, char *output_string, size_t output_size)
{
    float midi_value       = 69.0f + 12.0f * log2f(frequency_hz / 440.0f);
    int   midi_rounded     = (int)(midi_value + 0.5f);
    int   octave_number    = (midi_rounded / 12) - 1;
    const char *note_name  = note_names[midi_rounded % 12];
    float cents_deviation  = (midi_value - midi_rounded) * 100.0f;
    
    snprintf(output_string, output_size, "%s%d (%+.0f cents)", note_name, octave_number, cents_deviation);
}

static bool debounce_note_selection(int current_midi_note)
{
    if (current_midi_note == last_candidate_midi) {
        candidate_repeat_count++;
    } else {
        last_candidate_midi    = current_midi_note;
        candidate_repeat_count = 1;
    }
    return (candidate_repeat_count >= NOTE_DEBOUNCE_FRAMES);
}

/* ============================================================
 * TASKS & INIT
 * ============================================================ */

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

static void init_dsp_library(void)
{
    esp_err_t ret = dsps_fft2r_init_fc32(NULL, CONFIG_DSP_MAX_FFT_SIZE);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Not possible to initialize FFT. Error = %i", ret);
        return;
    }
    dsps_wind_hann_f32(fft_window, FFT_SIZE);
}

static void init_continuous_adc(adc_continuous_handle_t *out_handle)
{
    adc_continuous_handle_cfg_t handle_configuration = {
        .max_store_buf_size = 4096,
        .conv_frame_size    = READ_LENGTH,
    };
    ESP_ERROR_CHECK(adc_continuous_new_handle(&handle_configuration, out_handle));

    adc_continuous_config_t digital_configuration = {
        .sample_freq_hz = SAMPLE_RATE,
        .conv_mode      = ADC_CONV_SINGLE_UNIT_1,
        .format         = ADC_DIGI_OUTPUT_FORMAT_TYPE1,
        .pattern_num    = 1,
    };

    static adc_digi_pattern_config_t adc_pattern = {
        .atten     = ADC_ATTEN_DB_12,
        .channel   = ADC_CHANNEL_6,
        .unit      = ADC_UNIT_1,
        .bit_width = ADC_BITWIDTH_12,
    };
    digital_configuration.adc_pattern = &adc_pattern;
    ESP_ERROR_CHECK(adc_continuous_config(*out_handle, &digital_configuration));
}

/* ============================================================
 * DSP
 * ============================================================ */

static void run_fft_frame(void)
{
    for (int i = 0; i < FFT_SIZE; i++) {
        float centered_sample      = remove_dc_offset(sample_buffer[i]);
        fft_input[2*i]             = centered_sample * fft_window[i];   
        fft_input[2*i + 1]         = 0.0f;                              
    }

    dsps_fft2r_fc32(fft_input, FFT_SIZE);
    dsps_bit_rev_fc32(fft_input, FFT_SIZE);

    float sum_magnitude  = 0.0f;

    for (int i = 1; i < FFT_SIZE / 2; i++) {     
        float real_part      = fft_input[2*i];
        float imaginary_part = fft_input[2*i + 1];
        float current_magnitude = sqrtf(real_part*real_part + imaginary_part*imaginary_part);
        
        fft_magnitude[i] = current_magnitude;
        sum_magnitude   += current_magnitude;
    }

    // Harmonic Product Spectrum: collapse overtones onto the true fundamental
    float peak_hps_value = 0.0f;
    int   peak_bin_index = 0;
    for (int i = 1; i < (FFT_SIZE / 2) / HPS_HARMONICS; i++) {
        float hps_product = fft_magnitude[i];
        for (int h = 2; h <= HPS_HARMONICS; h++) {
            hps_product *= fft_magnitude[i * h];
        }
        if (hps_product > peak_hps_value) {
            peak_hps_value = hps_product;
            peak_bin_index = i;
        }
    }

    float peak_magnitude = fft_magnitude[peak_bin_index];
    float average_magnitude = sum_magnitude / (float)(FFT_SIZE / 2 - 1);
    float signal_to_noise_ratio = (average_magnitude > 0.0f) ? (peak_magnitude / average_magnitude) : 0.0f;

    if (hold_start_time == 0) hold_start_time = esp_timer_get_time();

    if (peak_magnitude > hold_peak_magnitude) {
        hold_peak_magnitude = peak_magnitude;
        hold_peak_bin_index = peak_bin_index;
        hold_peak_signal_to_noise_ratio = signal_to_noise_ratio;
    }
}

/* ============================================================
 * MAIN
 * ============================================================ */

void app_main(void)
{
    xTaskCreate(blink_heartbeat_task, "blink_task", 2048, NULL, 5, NULL);
    init_dsp_library();
    
    adc_continuous_handle_t adc_handle = NULL;  
    init_continuous_adc(&adc_handle);
    ESP_ERROR_CHECK(adc_continuous_start(adc_handle));

    // Moved off the stack to static memory to prevent overflows
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
                uint32_t signal_amplitude_mv = get_peak_to_peak_mv(sample_buffer, FFT_SIZE, &avg_adc_val);

                // Only run FFT if the signal crosses the noise floor threshold
                if (signal_amplitude_mv > NOTE_ACTIVATION_THRESHOLD_MV) {
                    run_fft_frame();
                    
                    int64_t current_time = esp_timer_get_time();
                    if ((current_time - hold_start_time) >= (HOLD_WINDOW_MS * 1000)) {
                        
                        float frequency_hz = (float)hold_peak_bin_index * ((float)SAMPLE_RATE / (float)FFT_SIZE);
                        
                        // Enforce minimum magnitude, minimum SNR, and valid frequency range
                        if (hold_peak_magnitude >= MIN_VALID_MAGNITUDE && 
                            hold_peak_signal_to_noise_ratio >= MIN_PEAK_TO_AVERAGE_RATIO &&
                            frequency_hz >= 27.5f && frequency_hz <= 4186.0f) {
                            
                            int current_midi_note = (int)(69.0f + 12.0f * log2f(frequency_hz / 440.0f) + 0.5f);
                            
                            if (debounce_note_selection(current_midi_note)) {
                                char note_string[32];
                                frequency_to_note(frequency_hz, note_string, sizeof(note_string));
                                
                                ESP_LOGI(TAG, "FFT SUCCESS: %-10s | %7.1f Hz | Mag: %6.0f | VPP: %lu mV",
                                         note_string, frequency_hz, hold_peak_magnitude, signal_amplitude_mv);
                            }
                        }
                        
                        // Reset hold logic
                        hold_peak_magnitude = 0.0f;
                        hold_peak_bin_index = -1;
                        hold_peak_signal_to_noise_ratio = 0.0f;
                        hold_start_time = esp_timer_get_time();
                    }
                }
                
                sample_count = 0;
            }
        } else {    
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    }
}