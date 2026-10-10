// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#include <mach/kmod.h>
extern "C" {
    kern_return_t _start(kmod_info_t *, void *);
    kern_return_t _stop(kmod_info_t *, void *);
    KMOD_EXPLICIT_DECL(de.nordapps.LunarBridge, "0.2.0d1", _start, _stop)
    __attribute__((visibility("hidden"))) kmod_start_func_t *_realmain = nullptr;
    __attribute__((visibility("hidden"))) kmod_stop_func_t *_antimain = nullptr;
}
