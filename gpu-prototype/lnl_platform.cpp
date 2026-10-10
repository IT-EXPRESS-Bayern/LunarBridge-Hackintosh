// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Copyright 2013, 2023 Intel Corporation; underlying MIT notices are retained.
// See ../LICENSES/MIT-Intel.txt.
// Derived hardware metadata: Linux v6.17, pinned commit in SOURCE-NOTICES.txt.
#include "lnl_platform.h"

extern "C" int lnl_describe_pci(uint32_t vendor, uint32_t device, LnlDescriptor *out) {
    if (!out || vendor != 0x8086)
        return 0;
    switch (device) {
        case 0x6420:
        case 0x64a0:
        case 0x64b0:
            break;
        default:
            return 0;
    }
    // These are Linux platform limits, NOT the fused topology of this laptop.
    out->device_id = device;
    out->dma_address_bits = 46;
    out->max_gt_per_tile = 2;
    out->needs_scratch = 1;
    return 1;
}

extern "C" int lnl_decode_gmd(uint32_t raw, LnlGmdVersion *out) {
    if (!out || raw == 0 || raw == 0xffffffffu)
        return 0;
    const uint32_t architecture = (raw >> 22) & 0x3ffu;
    if (architecture == 0)
        return 0;
    out->architecture = architecture;
    out->release = (raw >> 14) & 0xffu;
    out->revision = raw & 0x3fu;
    out->version_x100 = architecture * 100u + out->release;
    // A decoded version does NOT establish supported engines, firmware or Metal.
    return 1;
}

extern "C" int lnl_gmd_read_offset(uint32_t gt, uint64_t mapped_bytes, uint32_t *out) {
    if (!out || (gt != LnlRenderGt && gt != LnlMediaGt))
        return 0;
    const uint32_t offset = 0x00000d8cu + (gt == LnlMediaGt ? 0x00380000u : 0u);
    // Compare after widening: no wraparound at the end of a mapping.
    if (mapped_bytes < static_cast<uint64_t>(offset) + sizeof(uint32_t))
        return 0;
    *out = offset;
    return 1;
}
