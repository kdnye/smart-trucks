#pragma once
// Passive BLE scanning + per-window RSSI aggregation (both roles scan).

#include <Arduino.h>
#include "frames.h"

// Start a continuous passive NimBLE scan. Window < interval leaves the shared
// radio time slices for Wi-Fi/ESP-NOW TX (coexistence).
void scanner_begin(int8_t rssi_floor);

// Update the advertisement RSSI floor at runtime (config push).
void scanner_set_floor(int8_t rssi_floor);

// Copy up to `max_out` aggregated device records (median/max RSSI per MAC in
// the current window) into `out`, then clear the window. Returns the count.
// `window_s_out` receives the seconds the window actually spanned.
// Call clear_after=false to snapshot without clearing (retry path: the window
// is only cleared once the batch is confirmed delivered).
size_t scanner_snapshot(DeviceRecord *out, size_t max_out, uint16_t *window_s_out,
                        bool clear_after);

// Clear the aggregation window (after a confirmed delivery).
void scanner_clear();

// Cumulative devices reported since boot (heartbeat stat).
uint32_t scanner_total_devices();
