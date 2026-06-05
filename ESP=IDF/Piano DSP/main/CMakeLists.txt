idf_component_register(SRCS "audio_dsp.c" "adc_sampler.c" "main.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_adc esp_timer esp_driver_gpio)