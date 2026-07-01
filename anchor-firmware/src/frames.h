#pragma once
// ESP-NOW wire format shared by anchor and gateway roles.
//
// ESP-NOW payloads are capped at 250 bytes, so scan batches use a packed
// binary layout (~9 bytes/device -> up to 24 devices per frame). All ints are
// little-endian (ESP32 native). Version bumps are additive: the gateway
// ignores frames whose version it does not understand.

#include <stdint.h>

static const uint8_t FRAME_MAGIC0 = 'F';
static const uint8_t FRAME_MAGIC1 = 'A';
static const uint8_t FRAME_VERSION = 1;

enum MsgType : uint8_t {
    MSG_SCAN_BATCH = 0x01,      // anchor -> gateway: aggregated BLE observations
    MSG_HEARTBEAT = 0x02,       // anchor -> gateway: liveness + stats
    MSG_GATEWAY_BEACON = 0x10,  // gateway -> broadcast: pairing/discovery
    MSG_CONFIG = 0x11,          // gateway -> broadcast: push settings to anchors
};

#pragma pack(push, 1)

struct FrameHeader {
    uint8_t magic0;
    uint8_t magic1;
    uint8_t version;
    uint8_t msg_type;
    uint32_t seq;
};

struct DeviceRecord {
    uint8_t mac[6];
    int8_t median_rssi;
    int8_t max_rssi;
    uint8_t sample_count;  // samples in this window, saturates at 255
};

static const uint8_t MAX_DEVICES_PER_FRAME = 24;

struct ScanBatchFrame {
    FrameHeader hdr;
    uint16_t window_s;   // seconds this aggregation window actually spanned
    uint8_t part;        // frame index within one snapshot (0-based)
    uint8_t part_count;  // total frames in this snapshot
    uint8_t device_count;
    DeviceRecord devices[MAX_DEVICES_PER_FRAME];

    size_t wire_size() const {
        return sizeof(ScanBatchFrame) - sizeof(devices) + device_count * sizeof(DeviceRecord);
    }
};

struct HeartbeatFrame {
    FrameHeader hdr;
    uint32_t uptime_s;
    uint32_t free_heap;
    uint32_t scan_devices_total;  // cumulative devices reported since boot
    uint16_t report_interval_s;
    int8_t rssi_floor;
    uint8_t channel;
    char fw[12];  // NUL-padded version string
};

struct GatewayBeaconFrame {
    FrameHeader hdr;
    uint8_t channel;
};

struct ConfigFrame {
    FrameHeader hdr;
    uint16_t report_interval_s;
    int8_t rssi_floor;
};

#pragma pack(pop)

inline void fill_header(FrameHeader &hdr, MsgType type, uint32_t seq) {
    hdr.magic0 = FRAME_MAGIC0;
    hdr.magic1 = FRAME_MAGIC1;
    hdr.version = FRAME_VERSION;
    hdr.msg_type = type;
    hdr.seq = seq;
}

inline bool header_valid(const uint8_t *data, int len) {
    if (len < (int)sizeof(FrameHeader)) return false;
    const FrameHeader *hdr = (const FrameHeader *)data;
    return hdr->magic0 == FRAME_MAGIC0 && hdr->magic1 == FRAME_MAGIC1 &&
           hdr->version == FRAME_VERSION;
}
