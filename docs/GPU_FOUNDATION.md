# Xe2-Speicher und GuC-Grundlagen

Die neuen C++-Bausteine sind eigenständiger, freestanding-kompilierbarer Kerncode.
Unter Windows ausgeführte DLL-Prüfungen benutzen synthetische Adressen und
Nachrichten. Das native Buildskript nimmt dieselben Bausteine in den Link auf.
Der genaue bereits bestätigte Linkstand steht in VALIDATION.json.
Der IOKit-Anschluss ruft sie bisher nicht für Hardware-Bindung oder GuC-Verkehr
auf. Es gibt weiterhin keine GPU-Warteschlange oder Metal-Beschleunigung.

## GPU-Speicher

`lb_gpu_memory.h/.cpp` implementiert getrennte Besitzer, begrenzte Reservierungen
und monotone Tokens für 48-Bit-GPU-Adressbereiche. Die unteren und oberen 64 KiB
bleiben ausgespart. Pro Reservierung sind höchstens 1 MiB, pro Adressraum 8 MiB
und 32 Einträge vorgesehen. Freigabe prüft Besitzer und Token; alte Tokens werden
nicht wiederverwendet. Überläufe, korrupte Buchhaltung und erschöpfte Bereiche
werden vor einer Änderung abgewiesen.

Das PTE-Encoding unterscheidet PPGTT-4K-Seiten, Directory-Einträge und GGTT.
Xe2-PAT-Bits 0–4 haben unterschiedliche Positionen; die Referenz reserviert
PAT-Indizes 16–19. GGTT verwendet eigene PAT-Bits und keine PPGTT-Schreibfreigabe.
Fünf Seitentabellenebenen mit Root-Level 4 werden geplant; die deklarierte
Lunar-Lake-GPU-VA bleibt 48 Bit. Der Tabellenplan zählt die benötigten CPU-
Stagingseiten einschließlich gekreuzter 2-MiB-, 1-GiB- und 512-GiB-Grenzen.

Scatter/Gather-Listen werden vollständig auf Seitenalignment, exakte Gesamtlänge,
Überlappungen, Ausgabegröße und den 46-Bit-DMA-Bereich geprüft. Erst danach werden
PTE-Werte in einen CPU-Stagingpuffer geschrieben. Der Code erzeugt keine DMA-
Adresse aus einem Userspace-Pointer und veröffentlicht keine physische Adresse.
Ein ausgewählter PAT-Index bestätigt keine programmierte Cache-Kohärenz.

Referenzen: [Xe-PTE-Bitfelder](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/regs/xe_gtt_defs.h),
[PTE/PDE-Encoding und VM](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_vm.c),
[Tabellenebenen](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_pt.c),
[Xe2-PAT-Tabelle](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_pat.c).

## GuC-Firmware und Nachrichten

`lb_guc_firmware.h/.cpp` liest den 128-Byte-CSS-Header mit expliziten Little-
Endian-Zugriffen. Größen werden vor Subtraktionen in 64 Bit geprüft. Header,
uCode und RSA-Block müssen innerhalb der Datei liegen; optionale Modulus- und
Exponentdaten dürfen gemäß Referenz fehlen. GuC-Major 70 und die Referenz-
Untergrenze 70.29.2 werden geprüft. Die für Lunar Lake festgehaltene Empfehlung
70.45.2 wird separat ausgewiesen. Buildername und Buildtext werden nicht kopiert.
Der Parser authentifiziert die Signatur nicht und behauptet weder passende
Firmware-Lizenz noch Plattform-/Submission-ABI-Kompatibilität.

`lb_guc_transport.h/.cpp` implementiert CTB/HXG-Encoding und Decoding samt CPU-
Schattenring. Die 64-Byte-Descriptorform, Fence-ID, 255-DWord-Payloadgrenze,
Ursprung, Nachrichtentyp, reservierte Bits, Ringwrap, Füllstand und unvollständige
Nachrichten werden geprüft. Ungültige Nachrichten verändern keine Leseposition.
Der Ausgang wird nur nach erfolgreicher Prüfung aktualisiert; unbenutzte Payload-
Felder werden auf null gesetzt. Ein Firmware-Antworttyp ist kein GPU-Abschluss.

Referenzen: [CSS-Layout](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_uc_fw_abi.h),
[GuC-Versionen und Größenprüfung](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_uc_fw.c),
[CTB-ABI](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/abi/guc_communication_ctb_abi.h),
[HXG-ABI](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/abi/guc_messages_abi.h).

## Anschluss an echte Hardware

Der [native IODMACommand-/Mapper-Anschluss](DMA_BACKEND.md) ist implementiert
und standardmäßig deaktiviert. Die tatsächliche Vorbereitung, Seiteneigentum
und Cache-Kohärenz müssen am Zielgerät qualifiziert werden. Es fehlen gepinnte Tabellen und
Directory-Verknüpfung, Root-Veröffentlichung, GGTT-/WOPCM-Belegung, PAT-
Programmierung, TLB-Invalidierung und Fehler-/Power-Reihenfolge. Der Schattenring
benötigt eigene Hardware-Memory-Barriers, GuC-Registrierung, Benachrichtigung,
Interrupts, reservierten G2H-Antwortraum, Fence-Lebensdauer und Wiederanlauf.

Die Bausteine sind nicht als untrusted Userspace-Blob-Schnittstelle ausgelegt.
Der künftige Kerneladapter besitzt und sperrt Arena, Segmentliste und Ring und
benutzt getrennte Ein-/Ausgabespeicher. Quellcodeprüfungen können deren tatsächliche
IOKit-/IOMMU- und GuC-Ausführung am Gerät nicht ersetzen. Die Kernel- und Metal-
Capabilities der Diagnose-Kext bleiben unverändert ohne GPU-Ausführungsbit.
