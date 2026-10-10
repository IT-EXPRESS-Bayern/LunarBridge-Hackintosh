// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Windows test DLL entry; no hardware access, allocation or loader actions.
#include <stdint.h>
extern "C" int DllMain(void *, uint32_t, void *) { return 1; }
