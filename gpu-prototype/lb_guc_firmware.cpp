// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// CSS offsets: Intel MIT Xe source, Copyright 2022 Intel Corporation.
#include "lb_guc_firmware.h"

namespace {
uint32_t read_le32(const uint8_t *data, uint32_t offset) {
    return uint32_t(data[offset]) | (uint32_t(data[offset + 1]) << 8) |
           (uint32_t(data[offset + 2]) << 16) | (uint32_t(data[offset + 3]) << 24);
}
}
extern "C" uint32_t lb_guc_parse_css(const uint8_t *data, uint64_t bytes,
                                      LBGuCFirmwareLayout *out) {
    if (!data || !out || bytes > LB_GUC_MAX_FILE) return LBFirmwareInput;
    if (bytes < 128) return LBFirmwareTruncated;
    const uint64_t header = read_le32(data, 4);
    const uint64_t size = read_le32(data, 24);
    const uint64_t key = read_le32(data, 28);
    const uint64_t modulus = read_le32(data, 32);
    const uint64_t exponent = read_le32(data, 36);
    // Compute in 64 bits before subtracting; hostile 32-bit size fields cannot
    // underflow or wrap into a plausible CSS/uCode/RSA extent.
    if (header != 32 + key + modulus + exponent || size <= header || !key ||
        key > 2048 || header > LB_GUC_MAX_FILE / 4 || size > LB_GUC_MAX_FILE / 4)
        return LBFirmwareLayout;
    const uint64_t ucode = (size - header) * 4;
    const uint64_t rsa = key * 4;
    if (ucode > 8 * 1024 * 1024) return LBFirmwareLayout;
    if (ucode > bytes - 128 || rsa > bytes - 128 - ucode) return LBFirmwareTruncated;
    const uint32_t release = read_le32(data, 64) & 0xffffff;
    if ((release >> 16) != 70 || release < 0x461d02) return LBFirmwareVersion;
    LBGuCFirmwareLayout result{};
    result.fileBytes = bytes;
    result.ucodeOffset = 128;
    result.ucodeBytes = ucode;
    result.rsaOffset = 128 + ucode;
    result.rsaBytes = rsa;
    result.uploadBytes = 128 + ucode;
    result.releaseVersion = release;
    result.submissionVersion = read_le32(data, 68) & 0xffffff;
    result.privateDataBytes = read_le32(data, 120);
    // Pinned Lunar Lake recommended release 70.45.2; this is metadata only.
    result.referenceRecommended = release >= 0x462d02;
    result.moduleType = read_le32(data, 0);
    result.moduleVendor = read_le32(data, 16);
    *out = result;
    return LBFirmwareOK;
}
