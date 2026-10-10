# Entwicklungsaufgaben

Ziel: Möglichst umfangreiche native Arc-130V-Unterstützung unter macOS Tahoe.
Windows bleibt intern, macOS kommt auf die externe USB-C-SSD, die Bootauswahl
und Firmware-UEFI-Secure-Boot bleiben Anforderungen. Eine VM ersetzt das Ziel nicht.

## Unter Windows umsetzen und prüfen

- [x] Öffentliches Repository einrichten, GPLv3 und Herkunftshinweise einsetzen.
- [x] Commits ausschließlich mit der genehmigten Identität IT-EXPRESS Bayern ausführen.
- [x] Geräteerkennung und GMD_ID-Auswertung implementieren und ausführen.
- [x] Samsung-I2C-Quellpatch erstellen und mit acht Prüfungen validieren.
- [x] Native Grafikroute und fehlende Anschlüsse dokumentieren.
- [x] Nativen IOKit-Diagnosetreiber als Quellcode mit enger Gerätezuordnung fertigstellen.
- [x] Registerzugriffe standardmäßig sperren und mit unter Windows geprüften Vorbedingungen versehen.
- [x] Nativen Speicheranschluss als Quellcode mit begrenzten, pro Verbindung isolierten CPU-Puffern erstellen.
- [x] Tracker und festen Trace-ABI mit Ereignisnummern, Status, Reihenfolge und begrenztem Ringpuffer implementieren.
- [x] macOS-Diagnosesammler und Trace-Auswertung einschließlich Fehlerbericht erstellen.
- [x] Speichergrenzen, Trace-Verlust, Überläufe und Abbruchpfade unter Windows prüfen.
- [x] Alle fünf nativen C++-Bausteine gegen die externen SDK-Header kompilieren.
- [x] Reproduzierbaren macOS-Build für eine echte Diagnose-Kext als Skript bereitstellen.
- [x] GitHub-macOS-Buildworkflow mit festgehaltenem SDK-Stand vorbereiten.
- [x] Installer für OpenCore mit Prüfung, Sicherung und Rücknahme implementieren.
- [x] Installer an isolierten Testverzeichnissen mit 18 Fehler-/Transaktionsprüfungen validieren.
- [x] Issue-Vorlage für native Fehler mit Commit, Phase und bereinigten Ereignissen erstellen.
- [ ] Entwicklungsstand, Tests und Quellen dokumentieren und unter der genehmigten Identität veröffentlichen.

## Native Validierung benötigt

- [ ] Diagnose-Kext mit einem macOS-Kext-Linker bauen und Mach-O-Bundle prüfen.
- [ ] GitHub-Buildartefakt herunterladen, Inhalt/Prüfsummen prüfen und als Diagnose-Build bereitstellen.
- [ ] Kext tatsächlich unter dem Ziel-Tahoe laden und Gerätezuordnung nachweisen.
- [ ] PCI-Daten, Grafik-/Media-IP und später echte Fusetopologie am Gerät erfassen.
- [ ] Speicheranschluss, Verbindungsschluss und parallele Clients auf macOS prüfen.
- [ ] Native Logs einschließlich Fehler-/Abbruchfällen mit dem Sammler erfassen.
- [ ] Trace-Erhalt bei Kernelpanic/Neustart untersuchen; Ringpuffer ist bisher flüchtig.
- [ ] UEFI-Secure-Boot-Vertrauenskette mit der Samsung-Firmware prüfen.
- [ ] Externe SSD und Installer erst nach gesicherter Datensicherung am realen Gerät verwenden.
- [ ] Bootauswahl, unveränderten Windows-Start und Rücknahme des Treibers prüfen.

## Weitere Hardware und Installation

- [ ] CPU-/XNU-Anpassung, SMBIOS, ACPI und Energieverwaltung für Lunar Lake prüfen.
- [ ] Recovery-Start von externer SSD ohne USB-Stick vorbereiten und nativ prüfen.
- [ ] OpenCore-EFI mit passender Gerätezuordnung und geprüfter Secure-Boot-Kette erstellen.
- [ ] Samsung-I2C-Patch als Kext bauen und Controller/GPIO/Interrupts qualifizieren.
- [ ] Touchpad IMG4100 und Goodix GXTP7936 über HID-over-I2C samt Standby prüfen.
- [ ] BE201-WLAN: Familie, Firmware und mögliche native Treiberportierung untersuchen.
- [ ] Bluetooth 8087:0037: Gen3-/TLV-Pfad und passende Firmware nativ prüfen.
- [ ] Audio: SST-Transport, ALC298 und MAX98390-Verstärkerpfad für Tahoe klären.
- [ ] PS/2-Tastatur und Funktionstasten mit passendem Treiber prüfen.
- [ ] xHCI, USB-Portzuordnung und USB4-/USB-C-SSD-Stabilität nativ prüfen.

## Grafikbeschleunigung: anschließende Implementierung

- [ ] Xe2-Speicherverwaltung: GGTT/PPGTT, GPU-VA, DMA/IOMMU und Seiteneigentum portieren.
- [ ] GuC-Firmwareformat/-Lizenz, Upload, Boot, CT-Kommunikation und Wiederanlauf umsetzen.
- [ ] Eine echte GPU-Warteschlange mit Abschlussnachweis und Timeout/Reset erstellen.
- [ ] Xe2-Compute-Shader ausführen und das Ergebnis byteweise vergleichen.
- [ ] Shader-Compilerpfad für Vulkan-Shader/AIR auswählen und nativ bauen.
- [ ] Nativen Metal-Geräteanschluss einschließlich Ressourcen und Synchronisierung implementieren.
- [ ] Metal-Minimaltest auf echter Hardware bestehen.
- [ ] IOAccel/IOSurface und Darstellung auf dem internen Panel integrieren.
- [ ] Display-Modi, Helligkeit und externe Displaywege prüfen.
- [ ] Standby, Aufwachen, Lastwechsel, Speicherfehler und Wiederanlauf qualifizieren.
- [ ] Weitere Engines, Video, zusätzliche Queues und Leistungsoptimierung erst nach Basistests erweitern.

Eine erledigte Quellcodeaufgabe belegt keine bestandene Hardwareprobe. Die Liste
wird nach tatsächlichen Ergebnissen aktualisiert. Die Beschleunigungsaufgaben
sind eine umfangreiche weitere Portierung; es gibt derzeit keinen bestätigten
nativen Arc-Metal-Treiber und keine Zusage fehlerfreier Funktion.

Aktuell bestanden: 59 Prüfungen unter Windows, sechs Compiler-Aufrufe und fünf
macOS-Objektdateien. Der macOS-Linkschritt und alle Geräteproben sind noch offen.
Der nächste unmittelbare Schritt ist der GitHub-Linkbuild; danach folgen
Matching/Snapshot und der CPU-Speichertest am Zielgerät. Erst danach wird der
GPU-Speicher-/GuC-/Queue-Pfad erweitert. Die Datensicherung der externen SSD ist
weiterhin nicht als abgeschlossen bestätigt.
