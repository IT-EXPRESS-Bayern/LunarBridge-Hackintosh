# Arc 130V: gemeinsame Plattform, verschiedene OEM-Subsysteme

**Für die erste Linux-Xe-Plattformzuordnung ist das OEM-Subsystem bei `8086:64A0` kein Auswahlkriterium. Für einen funktionierenden nativen macOS-Compute-Treiber reicht die gemeinsame Geräte-ID jedoch nicht aus.** Dieselbe Plattform ist eine brauchbare Entwicklungsreferenz; die konkrete Hardware muss weiterhin qualifiziert werden.

| Referenz | PCI Vendor:Device | Subsystem Vendor:Device | Beleggrenze |
|---|---|---|---|
| Verlinkter Linux-Hardware-Eintrag | `8086:64A0` | `1414:0092` | Öffentlich gelesener Datenbankeintrag, keine eigene Hardwareprobe |
| LunarBridge-Zielgerät Samsung Galaxy Book5 Pro 16 | `8086:64A0` | `144D:C1DD` | Bestehende Projektzielzuordnung; eigener Windows-OpenCL-Nachweis, noch kein nativer macOS-Dispatch |

## Was die verlinkte Seite belegt

[Der Nutzerlink](https://linux-hardware.org/?id=pci:8086-64a0-1414-0092) wurde am **11. Oktober 2026 um 09:33:20 UTC** beim ersten begrenzten direkten HTTP-Abruf erreicht: **HTTP 200**, 8.104 Bytes, 3,906 Sekunden. Die Web-Suchansicht lieferte separat HTTP 502. Der direkte Abruf genügte; es gab keine weiteren direkten Versuche.

Die Seite nennt Lunar Lake / Arc 130V–140V und führt `CONFIG_DRM_XE` für `8086:64A0` auf. Der letzte Status der dort enthaltenen Computerzeile lautet **`detected`**. Daraus folgen weder eine erfolgreiche Compute-Ausführung noch ein Metal- oder macOS-Nachweis. Einzelne Rechnerkennungen und Inventare werden hier nicht übernommen.

SHA-256 der vollständig gelesenen HTML-Antwort: `31bbebfcab95f61cc4dae8691da012651ed092d1a1e3ee1592a840a4466a9ca6`. Dies dokumentiert die gelesenen Bytes, ist jedoch keine Signatur des Datenbankbetreibers.

## Plattformzuordnung im gepinnten Linux-Code

Alle folgenden Primärstellen stammen aus Linux-Commit **`e5f0a698b34ed76002dc5cff3804a61c80233a7a`**. Die Zeilenangaben beziehen sich auf die vollständigen Rohdateien/Git-Dateien, nicht auf eine Webansicht, die Leerzeilen entfernt.

1. [`include/drm/intel/pciids.h`, Zeilen 841–844](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/include/drm/intel/pciids.h#L841-L844) definiert `INTEL_LNL_IDS` mit `6420`, **`64A0`** und `64B0`. `64A0` steht tatsächlich in **Zeile 843**; die Definition liegt direkt in diesem Header.
2. [`xe_pci.c`, Zeile 367](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_pci.c#L367) verwendet dieses Makro mit `INTEL_VGA_DEVICE` und `lnl_desc`. Der LNL-Deskriptor steht in Zeilen 312–319.
3. [`INTEL_VGA_DEVICE`, Zeilen 29–33](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/include/drm/intel/pciids.h#L29-L33) verwendet `PCI_DEVICE`. [`PCI_DEVICE`, Zeilen 1000–1002](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/include/linux/pci.h#L1000-L1002) setzt **Subvendor und Subdevice auf `PCI_ANY_ID`**. Die Display-Geräteklasse wird zusätzlich geprüft.

Damit fallen beide genannten Subsysteme in denselben initialen Linux-LNL-Match, sofern die Geräteklasse passt. Das ist eine Aussage über die Treibertabelle, keine Garantie vollständiger Initialisierung.

## Compute, Revision und Display getrennt prüfen

| Ebene | Was entscheidet? | Konsequenz für die Portierung |
|---|---|---|
| PCI-Zuordnung | Vendor, Device und passende Geräteklasse | Die gemeinsame Primary-ID ist ein konkreter Ansatzpunkt für wiederverwendbare LNL-Bausteine. |
| Graphics-/Media-IP | Tatsächlich gelesene GMD-Architektur, Release und Revision | Linux wählt die IP-Deskriptoren zur Laufzeit; `20.04` wird als `Xe2_LPG` geführt. Dies ist ein Quellcodewert, keine neue Messung auf dem Samsung. |
| Stepping | Separate Graphics-/Media-GMD-Revisionsfelder | PCI-Revision und GMD-Revision sind unterschiedliche Felder. Gleiche Primary-ID oder OEM-Kennung ersetzt keine Stepping-Prüfung. |
| Native Compute-Ausführung | Qualifizierte DMA-/GPU-Adressen, PAT, Besitz, Firmware, Queue und Abschlussprüfung | Diese Verträge müssen auf dem konkreten macOS-Zielgerät gelten. Ein anderer OEM-Eintrag erfüllt sie nicht für uns. |
| Display | Panel, VBT/OpRegion, Linkparameter, Stromsequenz und Helligkeitssteuerung | Board- und Paneldaten brauchen eigene Qualifikation, auch wenn der Compute-Plattformcode wiederverwendbar ist. |

Der GMD-Pfad dekodiert Architektur/Release/Revision und wählt Graphics-/Media-Deskriptoren. Unbekannte Graphics-IP beendet die Initialisierung; Media kann fehlen. Siehe [`xe_pci.c`, Zeilen 496–550 und 619–645](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_pci.c#L496-L550), [`GMD-Bitfelder`, Zeilen 37–40](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/regs/xe_gt_regs.h#L37-L40) und [`Stepping aus GMD`, Zeilen 222–244](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_step.c#L222-L244).

Der Xe-Displaypfad richtet die OpRegion ein: [`xe_display.c`, Zeilen 105–119](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/display/xe_display.c#L105-L119). Die Intel-Displayimplementierung liest VBT-Daten, bestimmt den Paneltyp und verarbeitet eDP-Link- und Stromsequenzparameter: [`intel_bios.c`, Zeilen 626–779, 1418–1549 und 3112–3135](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/i915/display/intel_bios.c#L1418-L1549).

**Schlussfolgerung für LunarBridge:** Das OEM-Subsystem ist in diesen Primärstellen kein Selektor der GMD-Graphics-IP. Die OEM-Kennung gehört zum Geräte-/Boardvertrag; gleiche Compute-Architektur beweist keine identischen Speicher-, Energie- oder Displaybedingungen. Solche Unterschiede sind zu prüfen und werden für diese beiden Geräte hier nicht behauptet oder gemessen.

## Aktueller nativer Gerätevertrag

Die eigene IOKit-Personality bleibt auf `IOPCIPrimaryMatch=0x64a08086` und `IOPCISecondaryMatch=0xc1dd144d` beschränkt. Die gleiche Samsung-Bedingung wird im Treiberstart und vor freigegebenen Forcewake-/MMIO-Zugriffen erneut geprüft: [Info.plist](../native/Info.plist), [LunarBridgeDriver.cpp](../native/LunarBridgeDriver.cpp) und [LBForceWakeAdapter.cpp](../native/LBForceWakeAdapter.cpp).

Eine spätere OEM-Erweiterung benötigt einen eigenen Gerätevertrag und echte native Tests. Es wurde keine Match-Bedingung geändert und für diese Quellenprüfung kein Hardwarezugriff ausgeführt. Der bestehende eigene Windows-OpenCL-Nachweis bleibt ausdrücklich Windows-Evidence; native Samsung-GPU-Ausführung und ein LunarBridge-Metal-Provider sind weiterhin unbestätigt. Weitere Voraussetzungen stehen in [GPU_BRINGUP.md](GPU_BRINGUP.md), [GGTT_BINDING.md](GGTT_BINDING.md), [GPU_DESIGN.md](GPU_DESIGN.md) und [PORTING_GUIDE.md](PORTING_GUIDE.md).

## SHA-256 der vollständig gelesenen Primärdateien

| Datei am genannten Linux-Pin | SHA-256 |
|---|---|
| `include/drm/intel/pciids.h` | `ae6ac8678e169f81669442a9ffc84f379b1a4db1a71cee448a4b34cfb937b079` |
| `include/linux/pci.h` | `fb47a85c0d6555013d193c318f2f12d30d067a245e37c75a551cd4c49def698f` |
| `drivers/gpu/drm/xe/xe_pci.c` | `1563c7f2b3d11aa5917bfc2660e6d5e05ad8b3c2086e6ffa4b6752a898040740` |
| `drivers/gpu/drm/xe/regs/xe_gt_regs.h` | `c028945c81fc65579f282c1614126a396ba784c87eafdb9cb10924bcb1653459` |
| `drivers/gpu/drm/xe/xe_step.c` | `aaeee59a1abb317586eff09602b36483b6794313cb1f8058e58402e8b7e3bc9b` |
| `drivers/gpu/drm/xe/display/xe_display.c` | `80a2a877fb305b79148a65022d50e3d4d46e44ce92e1363ec7432a0f5207729e` |
| `drivers/gpu/drm/i915/display/intel_bios.c` | `392151b4e9b3035a057c429cfc916ee23c6623a10fe2f29b8ac0929d240caf16` |

Die drei bereits manifestierten Dateien `pciids.h`, `xe_pci.c` und `xe_gt_regs.h` stimmen mit [reference-manifest.json](../tests/reference-manifest.json) überein. Weitere Dateien wurden nur für diese dokumentierte Quellenprüfung gelesen. Linux/Xe-/Intel-Displayquellen behalten ihre jeweiligen MIT-/GPL-Hinweise; diese Einordnung übernimmt keinen Treibercode und ändert keine Fremdlizenz.
