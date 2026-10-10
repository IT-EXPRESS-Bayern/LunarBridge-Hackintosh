// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#include <mach/kmod.h>
extern "C" {
    // x86_64 XNU initializes/finalizes C++ metaclasses during Kext load/unload.
    // No additional module-level operation is needed by this diagnostic service.
    __attribute__((visibility("hidden"))) kern_return_t _start(kmod_info_t *, void *) { return KERN_SUCCESS; }
    __attribute__((visibility("hidden"))) kern_return_t _stop(kmod_info_t *, void *) { return KERN_SUCCESS; }
    // Module identity for the external SDK metaclass interface.
    __attribute__((visibility("hidden"))) const char *OSKextGetCurrentIdentifier() { return "de.nordapps.LunarBridge"; }
    __attribute__((visibility("hidden"))) const char *OSKextGetCurrentVersionString() { return "0.2.0d1"; }
    KMOD_EXPLICIT_DECL(de.nordapps.LunarBridge, "0.2.0d1", _start, _stop)
}
