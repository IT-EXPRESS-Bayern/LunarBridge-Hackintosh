# Nativer Diagnose- und Speicheranschluss

Die Implementierung in `native/LunarBridgeDriver.cpp` besteht aus einem
`IOService` und einem administrativen `IOUserClient`. Ihre Quelltexte wurden
unter Windows gegen externe MacKernelSDK-Header zu x86_64-Mach-O-Objekten
kompiliert. Das bestätigt Kompilierbarkeit, keine Kext-Ladefähigkeit oder
binäre Kompatibilität mit dem Ziel-Tahoe.

Eigene minimale Moduleinstiege ersetzen eine statische SDK-Startroutine.
Der Kernel verwaltet C++-Initialisierung vor dem Moduleinstieg und Finalisierung
nach dem Stop-Pfad, wie im veröffentlichten
[XNU-OSKext-Start/Stop-Code](https://github.com/apple-oss-distributions/xnu/blob/main/libkern/c%2B%2B/OSKext.cpp)
beschrieben. Ob die konkrete Kext samt Metaclasses unter dem Ziel-Tahoe lädt,
bleibt eine erforderliche native Geräteprobe.

## Gerätezuordnung und Registerlesen

Die Personality trifft ausschließlich Intel `8086:64A0`, Samsung-Subsystem
`144D:C1DD`. Die eigene `IOMatchCategory=LunarBridgeDiagnostic` soll den
Diagnoseanschluss vom gewöhnlichen Accelerator-Matching unterscheiden.
Ob die gewünschte Anbindung auf dem tatsächlichen IOPCIDevice funktioniert,
muss im nativen Registrybaum geprüft werden. Kein Metal-Gerät wird angemeldet.

PCI-Konfiguration und BAR-Länge werden erfasst. MMIO ist standardmäßig aus.
Explizites `LBEnableMMIORead=true` erlaubt einen einmaligen Lesepfad nur nach:

1. Exakter Geräte-/Subsystemzuordnung und unterstützter Display-Klasse.
2. Gültiger PCI-Speicherfreigabe und vorhandener, lesbarer PM-Capability.
3. PCI-D0 und Provider-On-Zustand.
4. Ausreichender BAR-Länge für beide 32-Bit-GMD-Register.
5. Erfolgreichem Provider-Open und erneut geprüfter Konfiguration/Energiezustand.
6. Lesbarem BAR-Mapping, erneut geprüfter Länge und erneut geprüftem Zustand.

Gelesen werden nur Grafik-GMD bei `0xD8C` und Media-GMD bei `0x380D8C`.
Der Code schreibt keine PCI/MMIO-Register, aktiviert weder D0 noch Speicher-
freigabe selbst und führt kein Forcewake, Firmware-Upload oder GPU-Submit aus.
`LBActualMMIOPerformed` bezeichnet einen tatsächlich erreichten Quellcode-Lesepfad;
gültige GMD-Werte haben ein eigenes Capability-Bit. Keiner dieser Werte wurde
bisher am Zielgerät unter macOS erfasst.

Provider-Open und Zustandsprüfungen ersetzen keine qualifizierte Synchronisierung
mit PCI-Power-Transitions. Ein Wechsel zwischen Prüfung und Zugriff ist noch
nicht durch Hardwaretests ausgeschlossen. Deshalb bleibt der Lesepfad explizit
experimentell und standardmäßig gesperrt. Echte Fusetopologie wird noch nicht gelesen.

Die GMD-Adressen und Bitfelder stammen aus dem festgehaltenen
[Linux-Xe-Stand](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_pci.c).
Die Linux-Aufrufreihenfolge allein bestätigt keinen sicheren XNU-Energiepfad.

## ABI 1

| Selector | Eingabe | Ausgabe | Funktion |
| --- | --- | --- | --- |
| 0 | keine | Snapshot, 104 Byte | PCI-Daten, Policy, GMD und Fähigkeiten |
| 1 | ein uint64, gewünschte Bytezahl | ein uint64, gerundete Bytezahl | Ein eigener CPU-Puffer pro Verbindung |
| 2 | keine | BufferInfo, 24 Byte | Größe, Alignment, CPU-shared, GPU-bound=false |
| 3 | ein uint64, exklusiver Sequenzcursor | TraceBatch, 800 Byte | Maximal 16 Ereignisse; Cursor 0 beginnt beim ältesten |
| 4 | keine | keine | GPU-Submit gibt ausdrücklich Unsupported zurück |
| 5 | keine | DMAInfo, 32 Byte | Vorbereitete Bytezahl/Seiten/PTEs und Mapperstatus; keine Adressen |

Ein Trace-Eintrag hat 48 Byte. Längen werden durch den nativen Dispatch geprüft.
Indirekte Strukturdeskriptoren und asynchrone Aufrufe sind nicht unterstützt.
Reserved-Felder und unbenutzte Trace-Ausgaben werden auf null gesetzt.

## Puffer und Lebenszyklus

Bis zu acht gleichzeitige Clients erhalten jeweils höchstens einen Puffer von
1 MiB, auf 4 KiB gerundet. Das aktive Buchungsbudget beträgt 8 MiB. Puffer
werden vor Export genullt; jeder Client erhält nur seinen eigenen Deskriptor.
Es gibt keine physische oder DMA-Adressausgabe und keine GPU-Bindung.
Das explizite, standardmäßig deaktivierte `LBEnableDMAPreparation` ergänzt
einen [Geräte-Mapper-/IODMACommand-Anschluss](DMA_BACKEND.md), der Segmente
prüft und PTEs ausschließlich in CPU-Speicher vorbereitet.

Ein IOLock schützt Client-Zustand, ein zweiter im Service die Trace-/Budgetdaten.
Stop und Freigabe behandeln Client-Buchung und gehaltene Deskriptorreferenz
idempotent. Noch bestehende IOKit-Mappings können Referenzen halten; das aktive
Buchungsbudget ist daher keine bewiesene Obergrenze sämtlicher physisch noch
lebender Seiten. Verbindungstod, bestehende Mappings und parallele Clients
müssen auf macOS geprüft werden, bevor der Speicherpfad erweitert wird.

## Nächster Grafikschritt

Die CPU-seitigen GPU-VA-/PTE-/Tabellen- und GuC-Formatbausteine sind bereits
[implementiert und geprüft](GPU_FOUNDATION.md). Der native DMA-Adapter verwendet
Arena und PTE-Staging, veröffentlicht sie jedoch noch nicht an die GPU.
Tatsächliche Firmwarekommunikation ist noch nicht implementiert.

Nach nativer Diagnose-/Speicherqualifikation folgt Xe2-GPU-Speicherverwaltung
mit Seiteneigentum und GPU-VA. Danach werden GuC-Boot/Kommunikation und eine
Warteschlange mit echtem Abschlussnachweis benötigt. Erst ein byteweise geprüftes
Shader-Ergebnis eröffnet den Metal-Anschluss. Die vollständige Route und Quellen
stehen in [GPU_DESIGN.md](GPU_DESIGN.md); alle Geräteabnahmen sind noch offen.

API-Referenzen: [Apple IOKit Matching](https://developer.apple.com/library/archive/documentation/DeviceDrivers/Conceptual/IOKitFundamentals/Matching/Matching.html),
[IOPCIDevice](https://developer.apple.com/documentation/kernel/iopcidevice),
[IOUserClient::clientMemoryForType](https://developer.apple.com/documentation/kernel/iouserclient/3553394-clientmemoryfortype).
