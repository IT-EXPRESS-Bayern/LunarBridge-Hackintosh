# Licensing and commercial use

The project owner selected **GNU GPL version 3** on 2026-10-10. New project work
is licensed under `GPL-3.0-only`, unless an individual file explicitly retains
an upstream license. The repository LICENSE contains the full GPLv3 text.
Third-party notices and licenses are not replaced by this choice.

GPLv3 permits commercial use, sale and paid support. Distribution of covered
derived driver versions requires the applicable GPL notices and corresponding
source obligations. Other recipients can also redistribute and sell compliant
copies; GPLv3 does not reserve all commercial use to this project's owner.
See [the license, sections 4–6](https://opensource.org/license/gpl-3.0).

The commercial plan can therefore include development sponsorship, hardware
qualification, support and integration services. These are possible business
models, not current revenue, customers or working hardware claims.

An alternative proprietary license may be offered only for material for which
the offering party possesses every necessary right. GPL/BSD/MIT/LGPL source
obtained from others does not become solely owned by LunarBridge. Future outside
contributions need explicit rights planning before promising dual licensing.
The GPL option already granted to recipients cannot simply be revoked later.

## Component boundaries

| Component / reference | License / boundary |
| --- | --- |
| New LunarBridge prototype and tools | GPL-3.0-only |
| Intel Linux Xe hardware source underlying platform metadata | MIT; preserve Intel notices and permission text in `LICENSES/MIT-Intel.txt` |
| VoodooI2C source configuration patch | Upstream GNU GPL version 3; its exact license remains in `i2c-patch/LICENSE.txt` |
| WhateverXe / WhateverGreen | BSD-3-Clause reference; no source imported into the current prototype |
| Intel graphics compiler / compute-runtime | MIT references; integration would require their complete applicable notices |
| metal2vulkan | LGPL-3.0-or-later external candidate; no translator source linked into this prototype |
| Linux LPSS input downloaded for tests | External GPL-2.0-only test reference; not combined as kernel code or included in the public archive |
| MacKernelSDK | External headers, individually licensed; not relicensed by this repo |
| NootedGreen | Thou Shalt Not Profit 1.0; source reuse is excluded from the implementation |
| Apple/Windows binaries, firmware and installer media | No rights granted by LunarBridge; excluded from this public repo |

NootedGreen's pinned license requires a non-profit declaration covering
distribution and related support/services. That is incompatible with assuming
unrestricted commercial redistribution under GPL or Apache terms.
[Pinned license](https://github.com/sgiammori/NootedGreen/blob/95f81898be65cb9f7b3d370ca78054be4cc8a62d/LICENSE).

The name and logo are a separate branding question. A source license does not
establish trademark registration or grant ownership of third-party brands.
No trademark clearance has been performed for the proposed name LunarBridge.

Before distributing a working commercial driver, review the final dependency
and linkage graph, firmware redistribution terms and actual contributor rights.
This document records the current implementation choices; it is not a completed
legal review of a future product.
