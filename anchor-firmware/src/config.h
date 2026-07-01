#pragma once
// Persistent settings (NVS) + compile-time defaults for the FSI BLE anchor.

#include <Arduino.h>
#include <Preferences.h>

#ifndef FW_VERSION_STR
#define FW_VERSION_STR "0.0.0-dev"
#endif

enum class NodeRole : uint8_t {
    ANCHOR = 0,
    GATEWAY = 1,
};

struct Settings {
    NodeRole role = NodeRole::ANCHOR;
    // Wi-Fi channel shared by all nodes at a site. ESP-NOW peers must sit on
    // the same channel; the gateway's pairing beacon re-broadcasts it so a
    // mis-set anchor can still be steered from the gateway.
    uint8_t channel = 1;
    // Aggregation window: anchors snapshot + ship their per-device RSSI
    // aggregates every report_interval_s seconds.
    uint16_t report_interval_s = 15;
    // Ignore advertisements weaker than this (dBm). Keeps distant-parking-lot
    // noise out of the ESP-NOW budget.
    int8_t rssi_floor = -90;

    void load() {
        Preferences prefs;
        prefs.begin("fsi-anchor", true);
        role = prefs.getUChar("role", 0) == 1 ? NodeRole::GATEWAY : NodeRole::ANCHOR;
        channel = prefs.getUChar("channel", 1);
        report_interval_s = prefs.getUShort("interval", 15);
        rssi_floor = (int8_t)prefs.getChar("floor", -90);
        prefs.end();
        if (channel < 1 || channel > 13) channel = 1;
        if (report_interval_s < 5) report_interval_s = 5;
        if (report_interval_s > 300) report_interval_s = 300;
    }

    void save() const {
        Preferences prefs;
        prefs.begin("fsi-anchor", false);
        prefs.putUChar("role", role == NodeRole::GATEWAY ? 1 : 0);
        prefs.putUChar("channel", channel);
        prefs.putUShort("interval", report_interval_s);
        prefs.putChar("floor", rssi_floor);
        prefs.end();
    }
};

// "anchor-a1b2c3" — derived from the last 3 bytes of the factory MAC.
// Stable per device, matches ble_anchor_registry.anchor_id in the cloud.
inline String node_id() {
    uint64_t mac = ESP.getEfuseMac();
    char buf[16];
    snprintf(buf, sizeof(buf), "anchor-%02x%02x%02x",
             (uint8_t)(mac >> 24), (uint8_t)(mac >> 32), (uint8_t)(mac >> 40));
    return String(buf);
}
