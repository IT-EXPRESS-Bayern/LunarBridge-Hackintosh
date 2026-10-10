// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Hardware constants adapted from Linux v6.17, intel/pciids.h and xe_gt_regs.h.
// Copyright 2013, 2023 Intel Corporation; underlying MIT notices are retained.
// See ../LICENSES/MIT-Intel.txt and SOURCE-NOTICES.txt.
#pragma once
#include <stdint.h>

#ifdef _WIN32
#define LNL_API __declspec(dllexport)
#else
#define LNL_API
#endif

struct LnlDescriptor {
    uint32_t device_id;
    uint32_t dma_address_bits;
    uint32_t max_gt_per_tile;
    uint32_t needs_scratch;
};

struct LnlGmdVersion {
    uint32_t architecture;
    uint32_t release;
    uint32_t revision;
    uint32_t version_x100;
};

enum LnlGtKind : uint32_t { LnlRenderGt = 0, LnlMediaGt = 1 };

extern "C" {
LNL_API int lnl_describe_pci(uint32_t vendor, uint32_t device, LnlDescriptor *out);
// Parse a supplied value only: this function never accesses hardware.
LNL_API int lnl_decode_gmd(uint32_t raw, LnlGmdVersion *out);
// Check a future read against the size of an already mapped BAR.
// This function neither maps BARs nor performs MMIO reads or writes.
LNL_API int lnl_gmd_read_offset(uint32_t gt, uint64_t mapped_bytes, uint32_t *out);
}
