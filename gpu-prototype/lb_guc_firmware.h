// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// CSS layout: Intel MIT Xe source, Copyright 2022 Intel Corporation.
#pragma once
#include "lnl_platform.h"

constexpr uint64_t LB_GUC_MAX_FILE = 16 * 1024 * 1024;
enum LBFirmwareResult : uint32_t {
    LBFirmwareOK = 0, LBFirmwareInput = 1, LBFirmwareTruncated = 2,
    LBFirmwareLayout = 3, LBFirmwareVersion = 4
};
struct LBGuCFirmwareLayout {
    uint64_t fileBytes, ucodeOffset, ucodeBytes, rsaOffset, rsaBytes, uploadBytes;
    uint32_t releaseVersion, submissionVersion, privateDataBytes, referenceRecommended;
    uint32_t moduleType, moduleVendor, signatureAuthenticated, reserved;
};

// Parses CSS bounds and the pinned Xe GuC-70 release floor. It does not verify
// the RSA signature, submission-ABI compatibility, platform identity or license,
// authenticate with the boot ROM, upload bytes or start the GuC. Builder name
// and build text in the firmware are deliberately never copied to the output.
extern "C" LNL_API uint32_t lb_guc_parse_css(const uint8_t *data, uint64_t bytes,
                                            LBGuCFirmwareLayout *out);
