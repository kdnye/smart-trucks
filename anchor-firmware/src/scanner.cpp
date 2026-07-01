#include "scanner.h"

#include <NimBLEDevice.h>

// ── Aggregation table ────────────────────────────────────────────────────────
// Fixed-size table: 64 devices/window, up to 16 RSSI samples each (newest
// overwrite oldest round-robin). Median over samples beats mean under BLE
// multipath. Everything below the RSSI floor is dropped at the callback.

namespace {

constexpr size_t MAX_TABLE = 64;
constexpr size_t MAX_SAMPLES = 16;

struct AggEntry {
    bool used = false;
    uint8_t mac[6];
    int8_t samples[MAX_SAMPLES];
    uint8_t n_ring = 0;       // valid entries in samples[]
    uint8_t next = 0;         // ring write index
    uint16_t total_count = 0; // all sightings this window (saturating in record)
    int8_t max_rssi = -128;
};

AggEntry g_table[MAX_TABLE];
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;
volatile int8_t g_floor = -90;
uint32_t g_window_started_ms = 0;
uint32_t g_total_devices = 0;

void record_advert(const uint8_t *mac, int rssi) {
    if (rssi < g_floor) return;
    portENTER_CRITICAL(&g_mux);
    AggEntry *slot = nullptr;
    for (size_t i = 0; i < MAX_TABLE; i++) {
        if (g_table[i].used && memcmp(g_table[i].mac, mac, 6) == 0) {
            slot = &g_table[i];
            break;
        }
        if (slot == nullptr && !g_table[i].used) slot = &g_table[i];
    }
    if (slot != nullptr) {
        if (!slot->used) {
            memset(slot, 0, sizeof(*slot));
            slot->used = true;
            memcpy(slot->mac, mac, 6);
            slot->max_rssi = -128;
        }
        slot->samples[slot->next] = (int8_t)rssi;
        slot->next = (slot->next + 1) % MAX_SAMPLES;
        if (slot->n_ring < MAX_SAMPLES) slot->n_ring++;
        if (slot->total_count < UINT16_MAX) slot->total_count++;
        if ((int8_t)rssi > slot->max_rssi) slot->max_rssi = (int8_t)rssi;
    }
    portEXIT_CRITICAL(&g_mux);
}

int8_t median_of(const int8_t *src, uint8_t n) {
    int8_t sorted[MAX_SAMPLES];
    memcpy(sorted, src, n);
    // insertion sort — n <= 16
    for (uint8_t i = 1; i < n; i++) {
        int8_t v = sorted[i];
        int8_t j = i - 1;
        while (j >= 0 && sorted[j] > v) {
            sorted[j + 1] = sorted[j];
            j--;
        }
        sorted[j + 1] = v;
    }
    return sorted[n / 2];
}

class AdvertCallbacks : public NimBLEAdvertisedDeviceCallbacks {
    void onResult(NimBLEAdvertisedDevice *dev) override {
        // NimBLE addresses are little-endian internally; getNative() yields
        // the raw 6 bytes. Reverse into transmission (big-endian) order so the
        // wire MAC matches the AA:BB:CC:DD:EE:FF the Pi's scanner reports.
        const uint8_t *native = dev->getAddress().getNative();
        uint8_t mac[6];
        for (int i = 0; i < 6; i++) mac[i] = native[5 - i];
        record_advert(mac, dev->getRSSI());
    }
};

AdvertCallbacks g_callbacks;

}  // namespace

void scanner_begin(int8_t rssi_floor) {
    g_floor = rssi_floor;
    g_window_started_ms = millis();
    NimBLEDevice::init("");
    NimBLEScan *scan = NimBLEDevice::getScan();
    scan->setAdvertisedDeviceCallbacks(&g_callbacks, /*wantDuplicates=*/true);
    scan->setActiveScan(false);  // passive: RSSI only, no scan requests
    scan->setInterval(100);      // 100 ms interval / 60 ms window: ~60% duty,
    scan->setWindow(60);         // leaves radio time for ESP-NOW TX
    scan->setMaxResults(0);      // callback-only, no result buffering
    scan->start(0, nullptr);     // 0 = scan forever
}

void scanner_set_floor(int8_t rssi_floor) { g_floor = rssi_floor; }

size_t scanner_snapshot(DeviceRecord *out, size_t max_out, uint16_t *window_s_out,
                        bool clear_after) {
    portENTER_CRITICAL(&g_mux);
    size_t count = 0;
    for (size_t i = 0; i < MAX_TABLE && count < max_out; i++) {
        AggEntry &e = g_table[i];
        if (!e.used || e.n_ring == 0) continue;
        DeviceRecord &rec = out[count++];
        memcpy(rec.mac, e.mac, 6);
        rec.median_rssi = median_of(e.samples, e.n_ring);
        rec.max_rssi = e.max_rssi;
        rec.sample_count = e.total_count > 255 ? 255 : (uint8_t)e.total_count;
    }
    uint32_t span_ms = millis() - g_window_started_ms;
    if (window_s_out != nullptr) {
        uint32_t span_s = span_ms / 1000;
        *window_s_out = span_s > UINT16_MAX ? UINT16_MAX : (uint16_t)span_s;
    }
    if (clear_after) {
        for (size_t i = 0; i < MAX_TABLE; i++) g_table[i].used = false;
        g_window_started_ms = millis();
        g_total_devices += count;
    }
    portEXIT_CRITICAL(&g_mux);
    return count;
}

void scanner_clear() {
    portENTER_CRITICAL(&g_mux);
    size_t count = 0;
    for (size_t i = 0; i < MAX_TABLE; i++) {
        if (g_table[i].used) count++;
        g_table[i].used = false;
    }
    g_window_started_ms = millis();
    g_total_devices += count;
    portEXIT_CRITICAL(&g_mux);
}

uint32_t scanner_total_devices() { return g_total_devices; }
