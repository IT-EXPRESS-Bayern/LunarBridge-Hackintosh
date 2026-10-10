// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// CTB/HXG ABI fields: Intel MIT, Copyright 2014-2021 Intel Corporation.
#pragma once
#include "lnl_platform.h"

constexpr uint32_t LB_CT_MAX_DATA = 255;
enum LBCtResult : uint32_t {
    LBCtOK = 0, LBCtInput = 1, LBCtBadDescriptor = 2, LBCtSpace = 3,
    LBCtEmpty = 4, LBCtTruncated = 5, LBCtFormat = 6, LBCtOrigin = 7, LBCtType = 8
};
enum LBCtDirection : uint32_t { LBHostToGuC = 0, LBGuCToHost = 1 };
struct LBCtDescriptor { uint32_t head, tail, status, reserved[13]; };
struct LBCtMessage { uint32_t fence, count, data[LB_CT_MAX_DATA]; };
static_assert(sizeof(LBCtDescriptor) == 64, "GuC CT descriptor ABI");

// CPU-shadow ring codec only. The qualified hardware backend must separately
// implement GGTT binding, GuC registration, memory barriers, notification,
// interrupt handling, flow control and request/completion tracking. Neither
// a decoded GuC response nor transport success is a GPU completion fence.
// Caller serializes the descriptor/ring, uses separate message output storage,
// and snapshots device-produced data before this code reads it.
extern "C" {
LNL_API uint32_t lb_ct_encode(uint32_t direction, const LBCtMessage *message,
                            uint32_t *frame, uint32_t capacity);
LNL_API uint32_t lb_ct_decode(uint32_t direction, const uint32_t *frame,
                            uint32_t frameWords, LBCtMessage *out);
LNL_API uint32_t lb_ct_shadow_write(uint32_t direction, LBCtDescriptor *descriptor,
                                  uint32_t *ring, uint32_t ringWords, const LBCtMessage *message);
LNL_API uint32_t lb_ct_shadow_read(uint32_t direction, LBCtDescriptor *descriptor,
                                 const uint32_t *ring, uint32_t ringWords, LBCtMessage *out);
}
