#include "espnow_link.h"

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

#include "frames.h"

namespace {

const uint8_t BROADCAST_MAC[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
constexpr uint32_t GATEWAY_STALE_MS = 30000;  // beacon lost -> unlinked
constexpr int SEND_RETRIES = 3;
constexpr uint32_t SEND_ACK_TIMEOUT_MS = 50;

NodeRole g_role = NodeRole::ANCHOR;
GatewayFrameHandler g_frame_handler = nullptr;
ConfigHandler g_config_handler = nullptr;

// Anchor: learned gateway state.
uint8_t g_gateway_mac[6] = {0};
bool g_gateway_known = false;
volatile uint32_t g_last_beacon_ms = 0;
volatile int g_link_rssi = 0;

// Gateway: beacon pacing.
uint32_t g_last_beacon_tx_ms = 0;
uint32_t g_beacon_seq = 0;

// Delivery confirmation handshake between esp_now_send and the send cb.
volatile bool g_send_pending = false;
volatile bool g_send_ok = false;

// ── Promiscuous RSSI sniffer ─────────────────────────────────────────────────
// Arduino core 2.x's esp_now recv callback drops the RSSI, so sniff it from
// the raw 802.11 action frames instead: keep the last RSSI per source MAC in
// a tiny ring and look it up when the ESP-NOW callback fires.

struct SniffEntry {
    uint8_t mac[6];
    int rssi;
};
constexpr size_t SNIFF_SLOTS = 8;
SniffEntry g_sniff[SNIFF_SLOTS] = {};
volatile size_t g_sniff_next = 0;

void promiscuous_rx(void *buf, wifi_promiscuous_pkt_type_t type) {
    if (type != WIFI_PKT_MGMT) return;  // ESP-NOW rides on mgmt action frames
    const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t *)buf;
    // 802.11 header: addr2 (transmitter) at payload offset 10.
    const uint8_t *src = pkt->payload + 10;
    for (size_t i = 0; i < SNIFF_SLOTS; i++) {
        if (memcmp(g_sniff[i].mac, src, 6) == 0) {
            g_sniff[i].rssi = pkt->rx_ctrl.rssi;
            return;
        }
    }
    size_t slot = g_sniff_next;
    g_sniff_next = (g_sniff_next + 1) % SNIFF_SLOTS;
    memcpy(g_sniff[slot].mac, src, 6);
    g_sniff[slot].rssi = pkt->rx_ctrl.rssi;
}

int sniffed_rssi_for(const uint8_t *mac) {
    for (size_t i = 0; i < SNIFF_SLOTS; i++) {
        if (memcmp(g_sniff[i].mac, mac, 6) == 0) return g_sniff[i].rssi;
    }
    return 0;
}

// ── ESP-NOW callbacks ────────────────────────────────────────────────────────

void ensure_peer(const uint8_t *mac, uint8_t channel) {
    if (esp_now_is_peer_exist(mac)) return;
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, mac, 6);
    peer.channel = channel;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;
    esp_now_add_peer(&peer);
}

void on_recv(const uint8_t *src_mac, const uint8_t *data, int len) {
    if (!header_valid(data, len)) return;
    const FrameHeader *hdr = (const FrameHeader *)data;

    if (g_role == NodeRole::ANCHOR) {
        if (hdr->msg_type == MSG_GATEWAY_BEACON && len >= (int)sizeof(GatewayBeaconFrame)) {
            if (!g_gateway_known || memcmp(g_gateway_mac, src_mac, 6) != 0) {
                memcpy(g_gateway_mac, src_mac, 6);
                g_gateway_known = true;
                const GatewayBeaconFrame *beacon = (const GatewayBeaconFrame *)data;
                ensure_peer(src_mac, beacon->channel);
            }
            g_last_beacon_ms = millis();
            g_link_rssi = sniffed_rssi_for(src_mac);
        } else if (hdr->msg_type == MSG_CONFIG && len >= (int)sizeof(ConfigFrame)) {
            const ConfigFrame *cfg = (const ConfigFrame *)data;
            if (g_config_handler != nullptr) {
                g_config_handler(cfg->report_interval_s, cfg->rssi_floor);
            }
        }
        return;
    }

    // Gateway: forward anchor traffic up to the serial emitter.
    if (hdr->msg_type == MSG_SCAN_BATCH || hdr->msg_type == MSG_HEARTBEAT) {
        if (g_frame_handler != nullptr) {
            g_frame_handler(src_mac, sniffed_rssi_for(src_mac), data, len);
        }
    }
}

void on_sent(const uint8_t *mac, esp_now_send_status_t status) {
    (void)mac;
    g_send_ok = (status == ESP_NOW_SEND_SUCCESS);
    g_send_pending = false;
}

}  // namespace

void espnow_begin(const Settings &settings) {
    g_role = settings.role;

    WiFi.mode(WIFI_STA);
    WiFi.disconnect(true, true);  // no AP association: ESP-NOW only
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_promiscuous_rx_cb(&promiscuous_rx);
    esp_wifi_set_channel(settings.channel, WIFI_SECOND_CHAN_NONE);

    if (esp_now_init() != ESP_OK) {
        Serial.println("{\"type\":\"error\",\"error\":\"esp_now_init_failed\"}");
        delay(2000);
        ESP.restart();
    }
    esp_now_register_recv_cb(&on_recv);
    esp_now_register_send_cb(&on_sent);
    ensure_peer(BROADCAST_MAC, settings.channel);
}

void espnow_set_gateway_frame_handler(GatewayFrameHandler handler) {
    g_frame_handler = handler;
}

void espnow_set_config_handler(ConfigHandler handler) { g_config_handler = handler; }

void espnow_tick(const Settings &settings) {
    if (g_role != NodeRole::GATEWAY) return;
    uint32_t now = millis();
    if (now - g_last_beacon_tx_ms < 5000) return;
    g_last_beacon_tx_ms = now;
    GatewayBeaconFrame beacon = {};
    fill_header(beacon.hdr, MSG_GATEWAY_BEACON, g_beacon_seq++);
    beacon.channel = settings.channel;
    espnow_broadcast((const uint8_t *)&beacon, sizeof(beacon));
}

bool espnow_linked() {
    return g_gateway_known && (millis() - g_last_beacon_ms) < GATEWAY_STALE_MS;
}

int espnow_link_rssi() { return g_link_rssi; }

bool espnow_send_to_gateway(const uint8_t *data, size_t len) {
    if (!espnow_linked()) return false;
    for (int attempt = 0; attempt < SEND_RETRIES; attempt++) {
        g_send_pending = true;
        g_send_ok = false;
        if (esp_now_send(g_gateway_mac, data, len) != ESP_OK) {
            g_send_pending = false;
        } else {
            uint32_t started = millis();
            while (g_send_pending && millis() - started < SEND_ACK_TIMEOUT_MS) {
                delay(1);
            }
            if (g_send_ok) return true;
        }
        delay(10 * (attempt + 1));  // brief backoff before retry
    }
    return false;
}

void espnow_broadcast(const uint8_t *data, size_t len) {
    esp_now_send(BROADCAST_MAC, data, len);
}
