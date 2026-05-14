# SmartWaste MVD — ESP32 Firmware
# Sensor de nivel de llenado para contenedores de residuos
#
# Requisitos:
#   - ESP-IDF v5.1+
#   - ESP32 (no ESP32-S2/S3, por compatibilidad con deep sleep + RTC memory)
#
# Build:
#   idf.py set-target esp32
#   idf.py build
#   idf.py -p /dev/ttyUSB0 flash monitor

cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(smartwaste-sensor)
