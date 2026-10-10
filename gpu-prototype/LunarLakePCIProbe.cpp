// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Experimental IOKit integration function, not a complete or loadable kext.
// It reads PCI configuration only and does not claim the GPU as its provider.
#include <IOKit/pci/IOPCIDevice.h>
#include "lnl_platform.h"

bool inspectLunarLakePCI(IOPCIDevice *device, LnlDescriptor *out) {
    if (!device || !out)
        return false;
    const uint16_t vendor = device->configRead16(kIOPCIConfigVendorID);
    const uint16_t product = device->configRead16(kIOPCIConfigDeviceID);
    return lnl_describe_pci(vendor, product, out) != 0;
}
