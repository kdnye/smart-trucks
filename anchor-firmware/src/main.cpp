// FSI BLE anchor firmware — M5Stack ATOM Lite.
//
// Role "anchor":  scan BLE asset beacons -> aggregate per window -> ESP-NOW
//                 unicast to the gateway. The aggregation window is only
//                 cleared after confirmed delivery, so short gateway outages
//                 lose nothing (samples keep accumulating).
// Role "gateway": receive anchor frames -> NDJSON over USB serial to the Pi's
//                 anchor-bridge co-process. The gateway scans BLE too — it is
//                 one more vantage point.
//
// See ../README.md for provisioning, LED codes, and the serial protocol.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <FastLED.h>

#include <cstdarg>

#include "config.h"
#include "espnow_link.h"
#include "frames.h"
#include "scanner.h"

// ── Hardware (ATOM Lite) ─────────────────────────────────────────────────────
constexpr int PIN_LED = 27;    // single SK6812 status LED
constexpr int PIN_BUTTON = 39; // front button (input-only pin, external pull)

CRGB g_led[1];
Settings g_settings;
String g_node_id;

uint32_t g_scan_seq = 0;
uint32_t g_hb_seq = 0;
uint32_t g_last_report_ms = 0;
uint32_t g_last_heartbeat_ms = 0;
constexpr uint32_t HEARTBEAT_INTERVAL_MS = 60000;

// Gateway: anchors heard recently (for gateway_status).
struct SeenAnchor {
    uint8_t mac[6];
    uint32_t last_ms;
};
constexpr size_t MAX_SEEN = 16;
SeenAnchor g_seen[MAX_SEEN] = {};

// Gateway serial emit buffer: 24 devices * ~45 chars + envelope fits well.
char g_json[2048];

// ── LED ──────────────────────────────────────────────────────────────────────

void led_show(const CRGB &color) {
    g_led[0] = color;
    FastLED.show();
}

void led_status_tick() {
    if (g_settings.role == NodeRole::GATEWAY) {
        led_show(CRGB(8, 8, 8));  // dim white: gateway idle
    } else if (espnow_linked()) {
        led_show(CRGB(0, 12, 0));  // dim green: linked to gateway
    } else {
        // blink blue while searching for the gateway beacon
        led_show((millis() / 500) % 2 == 0 ? CRGB(0, 0, 40) : CRGB::Black);
    }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

String anchor_id_from_mac(const uint8_t mac[6]) {
    char buf[16];
    snprintf(buf, sizeof(buf), "anchor-%02x%02x%02x", mac[3], mac[4], mac[5]);
    return String(buf);
}

void format_mac(const uint8_t mac[6], char out[18]) {
    snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2],
             mac[3], mac[4], mac[5]);
}

void note_anchor_seen(const uint8_t mac[6]) {
    uint32_t now = millis();
    size_t oldest = 0;
    for (size_t i = 0; i < MAX_SEEN; i++) {
        if (memcmp(g_seen[i].mac, mac, 6) == 0) {
            g_seen[i].last_ms = now;
            return;
        }
        if (g_seen[i].last_ms < g_seen[oldest].last_ms) oldest = i;
    }
    memcpy(g_seen[oldest].mac, mac, 6);
    g_seen[oldest].last_ms = now;
}

size_t anchors_recent_count() {
    uint32_t now = millis();
    size_t n = 0;
    for (size_t i = 0; i < MAX_SEEN; i++) {
        if (g_seen[i].last_ms != 0 && now - g_seen[i].last_ms < 300000) n++;
    }
    return n;
}

// ── Gateway: NDJSON emit ─────────────────────────────────────────────────────

// Append printf-formatted text to g_json at `off`, clamping to the buffer.
// Returns false once the buffer is full (snprintf reports the would-be length
// on truncation, so an unchecked `sizeof - off` would underflow and overflow
// the buffer on the next write). On false, callers drop the whole line — a
// truncated frame is worse than a missing one (the bridge rejects bad JSON).
bool json_append(int &off, const char *fmt, ...) {
    if (off < 0 || off >= (int)sizeof(g_json)) return false;
    va_list args;
    va_start(args, fmt);
    int written = vsnprintf(g_json + off, sizeof(g_json) - off, fmt, args);
    va_end(args);
    if (written < 0 || written >= (int)sizeof(g_json) - off) {
        off = (int)sizeof(g_json);  // mark full
        return false;
    }
    off += written;
    return true;
}

// link_rssi_valid=false emits `"link_rssi":null` (the gateway's own scans).
void emit_scan_json(const String &anchor_id, uint32_t seq, uint8_t part,
                    uint8_t part_count, uint16_t window_s, bool link_rssi_valid,
                    int link_rssi, const DeviceRecord *devices, size_t count) {
    int off = 0;
    if (!json_append(off,
                     "{\"type\":\"anchor_scan\",\"anchor_id\":\"%s\","
                     "\"gateway_id\":\"%s\",\"seq\":%lu,\"part\":%u,"
                     "\"part_count\":%u,\"window_s\":%u,",
                     anchor_id.c_str(), g_node_id.c_str(), (unsigned long)seq,
                     part, part_count, window_s)) {
        return;
    }
    if (link_rssi_valid && link_rssi != 0) {
        if (!json_append(off, "\"link_rssi\":%d,", link_rssi)) return;
    } else {
        if (!json_append(off, "\"link_rssi\":null,")) return;
    }
    if (!json_append(off, "\"devices\":[")) return;
    for (size_t i = 0; i < count; i++) {
        char mac_str[18];
        format_mac(devices[i].mac, mac_str);
        if (off >= (int)sizeof(g_json) - 64) break;  // leave room for "]}"
        if (!json_append(off,
                         "%s{\"mac\":\"%s\",\"rssi\":%d,\"max_rssi\":%d,\"count\":%u}",
                         i == 0 ? "" : ",", mac_str, devices[i].median_rssi,
                         devices[i].max_rssi, devices[i].sample_count)) {
            return;
        }
    }
    if (!json_append(off, "]}")) return;
    Serial.println(g_json);
}

void emit_heartbeat_json(const String &anchor_id, const HeartbeatFrame &hb,
                         bool link_rssi_valid, int link_rssi) {
    char fw[13];
    memcpy(fw, hb.fw, 12);
    fw[12] = '\0';
    int off = 0;
    if (!json_append(off,
                     "{\"type\":\"anchor_heartbeat\",\"anchor_id\":\"%s\","
                     "\"gateway_id\":\"%s\",\"seq\":%lu,\"uptime_s\":%lu,"
                     "\"free_heap\":%lu,\"scan_devices_total\":%lu,"
                     "\"report_interval_s\":%u,\"rssi_floor\":%d,"
                     "\"channel\":%u,\"fw\":\"%s\",",
                     anchor_id.c_str(), g_node_id.c_str(),
                     (unsigned long)hb.hdr.seq, (unsigned long)hb.uptime_s,
                     (unsigned long)hb.free_heap,
                     (unsigned long)hb.scan_devices_total,
                     hb.report_interval_s, hb.rssi_floor, hb.channel, fw)) {
        return;
    }
    if (link_rssi_valid && link_rssi != 0) {
        if (!json_append(off, "\"link_rssi\":%d}", link_rssi)) return;
    } else {
        if (!json_append(off, "\"link_rssi\":null}")) return;
    }
    Serial.println(g_json);
}

void emit_gateway_status() {
    snprintf(g_json, sizeof(g_json),
             "{\"type\":\"gateway_status\",\"gateway_id\":\"%s\",\"fw\":\"%s\","
             "\"uptime_s\":%lu,\"free_heap\":%lu,\"anchors_recent\":%u,"
             "\"channel\":%u,\"report_interval_s\":%u,\"rssi_floor\":%d}",
             g_node_id.c_str(), FW_VERSION_STR, (unsigned long)(millis() / 1000),
             (unsigned long)ESP.getFreeHeap(), (unsigned)anchors_recent_count(),
             g_settings.channel, g_settings.report_interval_s,
             g_settings.rssi_floor);
    Serial.println(g_json);
}

// Gateway: an ESP-NOW frame arrived from an anchor.
void on_gateway_frame(const uint8_t src_mac[6], int link_rssi,
                      const uint8_t *data, int len) {
    note_anchor_seen(src_mac);
    const FrameHeader *hdr = (const FrameHeader *)data;
    String anchor_id = anchor_id_from_mac(src_mac);
    if (hdr->msg_type == MSG_SCAN_BATCH && len >= (int)(sizeof(ScanBatchFrame) - sizeof(((ScanBatchFrame *)0)->devices))) {
        const ScanBatchFrame *batch = (const ScanBatchFrame *)data;
        size_t count = batch->device_count;
        if (count > MAX_DEVICES_PER_FRAME) count = MAX_DEVICES_PER_FRAME;
        size_t expected = sizeof(ScanBatchFrame) - sizeof(batch->devices) +
                          count * sizeof(DeviceRecord);
        if ((size_t)len < expected) return;  // truncated frame
        emit_scan_json(anchor_id, batch->hdr.seq, batch->part, batch->part_count,
                       batch->window_s, true, link_rssi, batch->devices, count);
        led_show(CRGB(0, 0, 30));  // blue blip on traffic
    } else if (hdr->msg_type == MSG_HEARTBEAT && len >= (int)sizeof(HeartbeatFrame)) {
        const HeartbeatFrame *hb = (const HeartbeatFrame *)data;
        emit_heartbeat_json(anchor_id, *hb, true, link_rssi);
    }
}

// Anchor: gateway pushed new settings.
void on_config_push(uint16_t report_interval_s, int8_t rssi_floor) {
    if (report_interval_s >= 5 && report_interval_s <= 300) {
        g_settings.report_interval_s = report_interval_s;
    }
    g_settings.rssi_floor = rssi_floor;
    scanner_set_floor(rssi_floor);
    g_settings.save();
}

// ── Anchor: report + heartbeat ───────────────────────────────────────────────

HeartbeatFrame build_heartbeat() {
    HeartbeatFrame hb = {};
    fill_header(hb.hdr, MSG_HEARTBEAT, g_hb_seq++);
    hb.uptime_s = millis() / 1000;
    hb.free_heap = ESP.getFreeHeap();
    hb.scan_devices_total = scanner_total_devices();
    hb.report_interval_s = g_settings.report_interval_s;
    hb.rssi_floor = g_settings.rssi_floor;
    hb.channel = g_settings.channel;
    strncpy(hb.fw, FW_VERSION_STR, sizeof(hb.fw) - 1);
    return hb;
}

void anchor_report() {
    // Snapshot without clearing: the window is only cleared once every part
    // of the batch is confirmed delivered (store-and-retry).
    DeviceRecord records[MAX_DEVICES_PER_FRAME * 4];
    uint16_t window_s = 0;
    size_t total = scanner_snapshot(records, sizeof(records) / sizeof(records[0]),
                                    &window_s, /*clear_after=*/false);
    if (total == 0) return;
    if (!espnow_linked()) return;  // keep accumulating until a gateway appears

    uint8_t part_count = (total + MAX_DEVICES_PER_FRAME - 1) / MAX_DEVICES_PER_FRAME;
    uint32_t seq = g_scan_seq++;
    bool all_delivered = true;
    for (uint8_t part = 0; part < part_count; part++) {
        ScanBatchFrame frame = {};
        fill_header(frame.hdr, MSG_SCAN_BATCH, seq);
        frame.window_s = window_s;
        frame.part = part;
        frame.part_count = part_count;
        size_t start = (size_t)part * MAX_DEVICES_PER_FRAME;
        size_t n = total - start;
        if (n > MAX_DEVICES_PER_FRAME) n = MAX_DEVICES_PER_FRAME;
        frame.device_count = (uint8_t)n;
        memcpy(frame.devices, records + start, n * sizeof(DeviceRecord));
        if (!espnow_send_to_gateway((const uint8_t *)&frame, frame.wire_size())) {
            all_delivered = false;
            break;
        }
    }
    if (all_delivered) {
        scanner_clear();
        led_show(CRGB(0, 40, 0));  // green blip on successful report
    }
}

void gateway_self_report() {
    DeviceRecord records[MAX_DEVICES_PER_FRAME];
    uint16_t window_s = 0;
    size_t count = scanner_snapshot(records, MAX_DEVICES_PER_FRAME, &window_s,
                                    /*clear_after=*/true);
    if (count == 0) return;
    emit_scan_json(g_node_id, g_scan_seq++, 0, 1, window_s,
                   /*link_rssi_valid=*/false, 0, records, count);
}

// ── Serial provisioning / config ─────────────────────────────────────────────

void print_show() {
    snprintf(g_json, sizeof(g_json),
             "{\"type\":\"node_info\",\"anchor_id\":\"%s\",\"role\":\"%s\","
             "\"fw\":\"%s\",\"channel\":%u,\"report_interval_s\":%u,"
             "\"rssi_floor\":%d,\"linked\":%s,\"link_rssi\":%d}",
             g_node_id.c_str(),
             g_settings.role == NodeRole::GATEWAY ? "gateway" : "anchor",
             FW_VERSION_STR, g_settings.channel, g_settings.report_interval_s,
             g_settings.rssi_floor, espnow_linked() ? "true" : "false",
             espnow_link_rssi());
    Serial.println(g_json);
}

void broadcast_config() {
    ConfigFrame cfg = {};
    fill_header(cfg.hdr, MSG_CONFIG, g_hb_seq++);
    cfg.report_interval_s = g_settings.report_interval_s;
    cfg.rssi_floor = g_settings.rssi_floor;
    espnow_broadcast((const uint8_t *)&cfg, sizeof(cfg));
}

void handle_serial_line(String line) {
    line.trim();
    if (line.length() == 0) return;

    if (line.startsWith("{")) {
        // JSON command from the Pi bridge: {"cmd":"config",...} applies the
        // settings here and (gateway role) pushes them to all anchors.
        JsonDocument doc;
        if (deserializeJson(doc, line) != DeserializationError::Ok) return;
        const char *cmd = doc["cmd"] | "";
        if (strcmp(cmd, "config") == 0) {
            if (doc["report_interval_s"].is<int>()) {
                int v = doc["report_interval_s"].as<int>();
                if (v >= 5 && v <= 300) g_settings.report_interval_s = (uint16_t)v;
            }
            if (doc["rssi_floor"].is<int>()) {
                int v = doc["rssi_floor"].as<int>();
                if (v >= -120 && v <= 0) g_settings.rssi_floor = (int8_t)v;
            }
            scanner_set_floor(g_settings.rssi_floor);
            g_settings.save();
            if (g_settings.role == NodeRole::GATEWAY) broadcast_config();
            print_show();
        } else if (strcmp(cmd, "show") == 0) {
            print_show();
        }
        return;
    }

    // Human-friendly text commands over `pio device monitor`.
    if (line == "show") {
        print_show();
    } else if (line == "role gateway" || line == "role anchor") {
        g_settings.role = line.endsWith("gateway") ? NodeRole::GATEWAY : NodeRole::ANCHOR;
        g_settings.save();
        Serial.println("{\"type\":\"info\",\"info\":\"role saved; rebooting\"}");
        delay(200);
        ESP.restart();
    } else if (line.startsWith("channel ")) {
        int v = line.substring(8).toInt();
        if (v >= 1 && v <= 13) {
            g_settings.channel = (uint8_t)v;
            g_settings.save();
            Serial.println("{\"type\":\"info\",\"info\":\"channel saved; rebooting\"}");
            delay(200);
            ESP.restart();
        }
    } else if (line.startsWith("interval ")) {
        int v = line.substring(9).toInt();
        if (v >= 5 && v <= 300) {
            g_settings.report_interval_s = (uint16_t)v;
            g_settings.save();
            if (g_settings.role == NodeRole::GATEWAY) broadcast_config();
            print_show();
        }
    } else if (line.startsWith("floor ")) {
        int v = line.substring(6).toInt();
        if (v >= -120 && v <= 0) {
            g_settings.rssi_floor = (int8_t)v;
            scanner_set_floor(g_settings.rssi_floor);
            g_settings.save();
            if (g_settings.role == NodeRole::GATEWAY) broadcast_config();
            print_show();
        }
    } else if (line == "reboot") {
        ESP.restart();
    }
}

void poll_serial() {
    static String buffer;
    while (Serial.available() > 0) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (buffer.length() > 0) handle_serial_line(buffer);
            buffer = "";
        } else if (buffer.length() < 512) {
            buffer += c;
        }
    }
}

// Hold the front button through power-on to toggle role (no laptop needed).
void check_role_toggle_at_boot() {
    if (digitalRead(PIN_BUTTON) != LOW) return;
    uint32_t started = millis();
    while (digitalRead(PIN_BUTTON) == LOW) {
        if (millis() - started > 3000) {
            g_settings.role = g_settings.role == NodeRole::GATEWAY
                                  ? NodeRole::ANCHOR
                                  : NodeRole::GATEWAY;
            g_settings.save();
            for (int i = 0; i < 3; i++) {  // purple flashes: role toggled
                led_show(CRGB(40, 0, 40));
                delay(150);
                led_show(CRGB::Black);
                delay(150);
            }
            return;
        }
        delay(10);
    }
}

// ── Arduino entry points ─────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    pinMode(PIN_BUTTON, INPUT);
    FastLED.addLeds<SK6812, PIN_LED, GRB>(g_led, 1);
    led_show(CRGB(30, 20, 0));  // yellow: booting

    g_settings.load();
    check_role_toggle_at_boot();
    g_node_id = node_id();

    espnow_begin(g_settings);
    if (g_settings.role == NodeRole::GATEWAY) {
        espnow_set_gateway_frame_handler(&on_gateway_frame);
    } else {
        espnow_set_config_handler(&on_config_push);
    }
    scanner_begin(g_settings.rssi_floor);
    print_show();

    g_last_report_ms = millis();
    g_last_heartbeat_ms = millis();
}

void loop() {
    uint32_t now = millis();
    espnow_tick(g_settings);
    poll_serial();

    if (now - g_last_report_ms >= (uint32_t)g_settings.report_interval_s * 1000) {
        g_last_report_ms = now;
        if (g_settings.role == NodeRole::GATEWAY) {
            gateway_self_report();
        } else {
            anchor_report();
        }
    }

    if (now - g_last_heartbeat_ms >= HEARTBEAT_INTERVAL_MS) {
        g_last_heartbeat_ms = now;
        HeartbeatFrame hb = build_heartbeat();
        if (g_settings.role == NodeRole::GATEWAY) {
            // Gateway reports itself directly over serial + a status line.
            emit_heartbeat_json(g_node_id, hb, /*link_rssi_valid=*/false, 0);
            emit_gateway_status();
        } else {
            espnow_send_to_gateway((const uint8_t *)&hb, sizeof(hb));
        }
    }

    led_status_tick();
    delay(20);
}
