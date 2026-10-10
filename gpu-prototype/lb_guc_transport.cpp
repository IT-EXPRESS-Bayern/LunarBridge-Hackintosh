// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// CTB/HXG fields: Intel MIT source, Copyright 2014-2021 Intel Corporation.
#include "lb_guc_transport.h"

namespace {
uint32_t check_hxg(uint32_t direction, uint32_t hxg) {
    if (direction > LBGuCToHost) return LBCtInput;
    if ((hxg >> 31) != direction) return LBCtOrigin;
    const uint32_t type = (hxg >> 28) & 7;
    if (direction == LBHostToGuC) return (type == 0 || type == 2) ? LBCtOK : LBCtType;
    return (type == 1 || type == 3 || type == 5 || type == 6 || type == 7) ? LBCtOK : LBCtType;
}
uint32_t check_descriptor(const LBCtDescriptor *desc, uint32_t words) {
    if (!desc || words < 1024 || words > 32768 || (words & (words - 1))) return LBCtInput;
    if (desc->head >= words || desc->tail >= words || desc->status) return LBCtBadDescriptor;
    for (uint32_t i = 0; i < 13; ++i) if (desc->reserved[i]) return LBCtBadDescriptor;
    return LBCtOK;
}
}

extern "C" uint32_t lb_ct_encode(uint32_t direction, const LBCtMessage *message,
                                 uint32_t *frame, uint32_t capacity) {
    if (!message || !frame || !message->count || message->count > LB_CT_MAX_DATA ||
        message->fence > 65535 || direction > LBGuCToHost) return LBCtInput;
    if (capacity < message->count + 1) return LBCtSpace;
    const uint32_t status = check_hxg(direction, message->data[0]);
    if (status) return status;
    const uint32_t count = message->count, fence = message->fence;
    uint32_t payload[LB_CT_MAX_DATA]{};
    for (uint32_t i = 0; i < count; ++i) payload[i] = message->data[i];
    frame[0] = (fence << 16) | count;
    for (uint32_t i = 0; i < count; ++i) frame[i + 1] = payload[i];
    return LBCtOK;
}

extern "C" uint32_t lb_ct_decode(uint32_t direction, const uint32_t *frame,
                                 uint32_t words, LBCtMessage *out) {
    if (!frame || !out || words < 2 || words > 256 || direction > LBGuCToHost) return LBCtInput;
    if (frame[0] & 0xff00) return LBCtFormat;
    const uint32_t count = frame[0] & 255;
    if (!count || words != count + 1) return LBCtTruncated;
    const uint32_t status = check_hxg(direction, frame[1]);
    if (status) return status;
    LBCtMessage result{};
    result.fence = frame[0] >> 16;
    result.count = count;
    for (uint32_t i = 0; i < count; ++i) result.data[i] = frame[i + 1];
    *out = result;
    return LBCtOK;
}

extern "C" uint32_t lb_ct_shadow_write(uint32_t direction, LBCtDescriptor *desc,
                                       uint32_t *ring, uint32_t words, const LBCtMessage *message) {
    const uint32_t status = check_descriptor(desc, words);
    if (status) return status;
    if (!ring || !message) return LBCtInput;
    uint32_t frame[256]{};
    const uint32_t encoded = lb_ct_encode(direction, message, frame, 256);
    if (encoded) return encoded;
    const uint32_t length = message->count + 1;
    const uint32_t free = (desc->head - desc->tail - 1) & (words - 1);
    if (free < length) return LBCtSpace;
    for (uint32_t i = 0; i < length; ++i) ring[(desc->tail + i) & (words - 1)] = frame[i];
    desc->tail = (desc->tail + length) & (words - 1);
    return LBCtOK;
}

extern "C" uint32_t lb_ct_shadow_read(uint32_t direction, LBCtDescriptor *desc,
                                      const uint32_t *ring, uint32_t words, LBCtMessage *out) {
    const uint32_t status = check_descriptor(desc, words);
    if (status) return status;
    if (!ring || !out || direction > LBGuCToHost) return LBCtInput;
    const uint32_t available = (desc->tail - desc->head) & (words - 1);
    if (!available) return LBCtEmpty;
    const uint32_t header = ring[desc->head];
    if (header & 0xff00) return LBCtFormat;
    const uint32_t length = (header & 255) + 1;
    if (length < 2) return LBCtFormat;
    if (length > available) return LBCtTruncated;
    uint32_t frame[256]{};
    for (uint32_t i = 0; i < length; ++i) frame[i] = ring[(desc->head + i) & (words - 1)];
    const uint32_t decoded = lb_ct_decode(direction, frame, length, out);
    if (decoded) return decoded;
    desc->head = (desc->head + length) & (words - 1);
    return LBCtOK;
}
