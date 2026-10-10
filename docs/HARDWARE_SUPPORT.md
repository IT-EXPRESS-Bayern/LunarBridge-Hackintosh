# Hardware support status

Source audit on 2026-10-10. Target laptop: Samsung Galaxy Book5 Pro 16,
NP960XHA, Core Ultra 5 226V, Lunar Lake. No native macOS boot has been tested.

| Device | Comparison / candidate | Current conclusion |
| --- | --- | --- |
| Arc 130V `8086:64A0` | Ice Lake Apple drivers; Xe-LP WhateverXe; Linux Xe/IGC | No verified Arc Metal support. Native Xe2 port research; metadata prototype only |
| I2C `8086:A878`–`A87B` | VoodooI2C and Linux Intel LPSS/DesignWare | Concrete source patch with model restriction and verified timing references |
| Touchpad `IMG4100` | VoodooI2CHID HID-over-I2C | Firmware exposes `PNP0C50`; controller/interrupt/sleep behavior still untested |
| Goodix touch `GXTP7936` | VoodooI2CHID HID-over-I2C | Same protocol-level candidate; device descriptors and interrupts require native testing |
| BE201 Wi-Fi `8086:A840` | itlwm/AX210; AirportItlwm-Tahoe | No published verified BE201 path found. A newer firmware/device-family port is required; AX210 support is not BE201 support |
| Bluetooth `8087:0037` | IntelBluetoothFirmware Gen3 and Linux btusb | Source matching candidate; existing Gen3 paths must be checked against this device's TLV version and firmware, not only USB ID |
| ALC298 behind SST `8086:A828`, MAX98390 amplifiers | AppleALC ALC298 layouts; SST and I2C amplifier drivers | Codec name alone does not solve SST transport; Tahoe removed AppleHDA, so the older AppleALC analog path is incomplete |
| PS/2 keyboard | VoodooPS2 | Plausible existing driver candidate; no native test |
| xHCI `8086:A87D`, `A831`; USB4 `A833` | Apple USB stack and USBToolBox | USBToolBox maps ports; it does not implement a missing xHCI/USB4 controller driver |
| External Lexar USB SSD | USB mass-storage bridge and native storage stack | Installation target; actual native controller/storage stability untested |

The I2C patch adds PCI matches `0xa8788086` through `0xa87b8086` and the Samsung
subsystem `0xc1dd144d`. Linux assigns all four controllers to `ehl_i2c_info`,
which uses a 100 MHz controller clock, 42 ns SDA hold, 171 ns SDA falling and
208 ns SCL falling timings. These are controller timings, not the touch bus rate.

The read firmware placed the touchpad at `_SB.PC00.I2C0.SPTP` and the touchscreen
at `_SB.PC00.I2C1.STSP`. Both expose compatible ID `PNP0C50` and HID-over-I2C
device-specific methods. Addresses/interrupts can be rewritten by `_INI` using
firmware variables, so the template addresses are not claimed as live values.
MAX98390 amplifiers appear on I2C2, reinforcing that audio needs more than a
generic Realtek layout. Full machine ACPI tables are excluded from this repo.

## I2C patch use

Base VoodooI2C source commit:
[`5c0c99cc451b03790c684ce6552c3f61620ad46b`](https://github.com/VoodooI2C/VoodooI2C/tree/5c0c99cc451b03790c684ce6552c3f61620ad46b).
Apply the patch to that full source checkout, then build its dependencies and
kext using its documented macOS build workflow. The patch was checked with
`git apply --check` and eight host-side tests; it has not been built as a kext.

The downloaded VoodooI2C 2.9.1 release uses a different controller arrangement.
Do not copy this source Info.plist into that release and assume compatibility.
Interrupt/GPIO, power and HID device validation remain necessary.

## Sources

- [Dortania Tahoe notes](https://dortania.github.io/OpenCore-Install-Guide/extras/tahoe.html)
- [OpenIntelWireless Wi-Fi support](https://openintelwireless.github.io/itlwm/Compat.html)
- [OpenIntelWireless Bluetooth support](https://openintelwireless.github.io/IntelBluetoothFirmware/Compat.html)
- [AirportItlwm-Tahoe](https://github.com/kgp-macPro/AirportItlwm-Tahoe)
- [Linux Intel LPSS source](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/mfd/intel-lpss-pci.c)
