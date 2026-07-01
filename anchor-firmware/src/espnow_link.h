#pragma once
// ESP-NOW plumbing for both roles.
//
// Gateway: broadcasts a pairing beacon every few seconds; anchors that hear
// it lock onto the gateway's MAC (zero-config pairing) and unicast their
// frames to it. Link RSSI is sniffed via a promiscuous RX hook (the classic
// ESP-NOW RSSI workaround on Arduino core 2.x) and doubles as a free
// anchor<->gateway ranging signal.

#include <Arduino.h>

#include "config.h"

// Gateway side: called for every valid anchor frame (scan batch / heartbeat).
// link_rssi is the sniffed RSSI of that frame at the gateway (0 if unknown).
using GatewayFrameHandler = void (*)(const uint8_t src_mac[6], int link_rssi,
                                     const uint8_t *data, int len);

// Anchor side: called when the gateway pushes new settings.
using ConfigHandler = void (*)(uint16_t report_interval_s, int8_t rssi_floor);

void espnow_begin(const Settings &settings);
void espnow_set_gateway_frame_handler(GatewayFrameHandler handler);
void espnow_set_config_handler(ConfigHandler handler);

// Periodic work (gateway beacon). Call from loop().
void espnow_tick(const Settings &settings);

// Anchor: true once a gateway beacon was heard recently (<30 s).
bool espnow_linked();
// Anchor: RSSI of the most recent gateway beacon (0 if never heard).
int espnow_link_rssi();

// Anchor: unicast a frame to the learned gateway with bounded retry.
// Returns true only on MAC-layer delivery confirmation.
bool espnow_send_to_gateway(const uint8_t *data, size_t len);

// Gateway: broadcast a frame (beacon / config push) to all anchors.
void espnow_broadcast(const uint8_t *data, size_t len);
