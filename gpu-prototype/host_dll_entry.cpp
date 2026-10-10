// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Windows test DLL entry; no hardware access, allocation or loader actions.
#include <stdint.h>
#include <stddef.h>
extern "C" int DllMain(void *, uint32_t, void *) { return 1; }
// Freestanding host-test support; volatile stores avoid recursive builtin lowering.
extern "C" void *memset(void *pointer, int value, size_t bytes) {
    volatile unsigned char *p = static_cast<volatile unsigned char *>(pointer);
    for (size_t i = 0; i < bytes; ++i) p[i] = static_cast<unsigned char>(value);
    return pointer;
}
extern "C" void *memcpy(void *to, const void *from, size_t bytes) {
    volatile unsigned char *p = static_cast<volatile unsigned char *>(to);
    const volatile unsigned char *q = static_cast<const volatile unsigned char *>(from);
    for (size_t i = 0; i < bytes; ++i) p[i] = q[i];
    return to;
}
