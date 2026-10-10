# LunarBridge-Hackintosh

Experimentelle Entwicklung nativer macOS-Treiber für Intel Lunar Lake.
Die laufende Umsetzung und offene Geräteproben stehen in der [Aufgabenliste](TASKS.md).
Das erste Ziel ist die Intel Arc 130V (`8086:64A0`) im Samsung Galaxy Book5 Pro.
Das Projekt enthält eigene native Diagnose-/CPU-Speicherbausteine, einen
Installer und Tests unter Windows. **Ein funktionierender Arc-Metal-Treiber, eine bootfähige
EFI und eine installierbare Grafik-Kext liegen noch nicht vor.**

[Geprüfter Stand](docs/VALIDATION.md): **59 bestandene Prüfungen unter Windows**,
sechs erfolgreiche Compiler-Aufrufe und fünf macOS-Objektdateien.
**Keine bestandene native macOS-Geräteprobe**; der GitHub-Linkbuild ist vorbereitet.

## Vorhandene Ergebnisse

| Baustein | Ergebnis | Aussage der Prüfung |
| --- | --- | --- |
| PCI-Erkennung für Lunar Lake | Eigener C++-Baustein für drei Linux-gelistete Geräte-IDs | Kompilierbar und unter Windows ausführbar |
| GMD_ID-Auswertung | Grafik-/Media-Registeradressen, Versionsfelder und Bereichsprüfung | Synthetische Registerwerte und Grenzfälle prüfbar; kein echter Registerzugriff |
| Nativer IOKit-Diagnosetreiber | Enge Samsung-/Arc-Zuordnung, PCI-Snapshot und expliziter GMD-Lesepfad mit Vorbedingungen | Quellcode als macOS-Objektdatei kompiliert; kein nativer Register-/Gerätetest |
| Nativer CPU-Speicheranschluss | Administrativer User-Client, eigene genullte Puffer und Größen-/Clientgrenzen | Quellcode kompiliert, portable Speicherregeln unter Windows ausgeführt; native Mappings ungeprüft |
| Tracker und Traces | Fester ABI, begrenzter Ereignisring, Sammler und Fehlerbericht | C++-Ringpuffer und simulierte IOKit-/Auswertungsfälle geprüft |
| OpenCore-Installer | Planen, sichern, installieren und zurücknehmen; Prüfsummen und Änderungserkennung | 18 Prüfungen an künstlichen EFI-Verzeichnissen; keine echte EFI geändert |
| Samsung-I2C | Quellpatch für vier Controller mit Samsung-Subsystemfilter und Linux-basierten Timings | Konfiguration und Gerätezuordnung prüfbar; keine native Geräteprobe |
| Grafikbeschleunigung, Display und Metal | Architekturvergleich und mögliche Portierungsstellen | Noch keine native Hardwarevalidierung |

Die Windows-Treiberkomponenten wurden lokal anhand ihrer Dateimetadaten und
Import-/Exporttabellen verglichen. Ihr Quellcode wurde dadurch nicht gewonnen.
Das öffentliche Repository enthält keine Windows- oder Apple-Treiberdateien.

## Untersuchte native Grafikroute

Der untersuchte Weg verbindet eine mögliche Metal-Übersetzung, einen
Xe2-fähigen Shader-Compiler, einen neuen nativen macOS-Anschluss und portierte
Teile des Linux-Xe-Treibers. Der Diagnose-/CPU-Pufferanschluss liegt als eigener
Quellcode vor; GPU-VA, GuC, Ausführungswarteschlangen und Metal-Anbindung fehlen.
[Architektur und konkrete Quellstellen](docs/GPU_DESIGN.md)
erklären, welche Teile als Referenz dienen und welche noch fehlen.

Ein erster Versuch könnte sich auf eine Ausführungswarteschlange und begrenzte
Puffer konzentrieren. Weniger Funktionen ändern jedoch nicht die
Xe2-Befehlssprache und die Abhängigkeiten von Speicherverwaltung und Firmware.
Eine zusätzliche Geräte-ID in einem alten Xe-LP-Treiber reicht daher nicht aus.

Der I2C-Patch ist ein gesonderter, kleinerer Ansatz. Er ist gegen einen
festgehaltenen VoodooI2C-Quellstand erstellt und noch kein geprüfter
Touchpad-/Touchscreen-Treiber. Die [Hardwareübersicht](docs/HARDWARE_SUPPORT.md)
nennt die vorhandenen Ansätze und ihre offenen Fragen.

## Ziel und Abnahme

Die Vorgaben bleiben: natives macOS Tahoe auf einer externen USB-C-SSD,
Windows auf dem internen Laufwerk, Auswahl beim Start und unterstützter
Firmware-UEFI-Secure-Boot. Eine VM ersetzt dieses Ziel nicht.
Apples SecureBootModel bestätigt keinen aktivierten Firmware-Secure-Boot.
Boot, Secure Boot, Metal-Ausführung, internes Display und Standby sind
jeweils getrennt am echten Gerät nachzuweisen; bisher ist keiner dieser
nativen Abnahmetests bestanden.

## Bauen und prüfen

Python 3, Zig 0.15.2 und externe MacKernelSDK-Header sind erforderlich.
Die Befehle stehen in der [englischen README](README.md#build-and-check-under-windows).
Die Referenzdateien werden mit festgehaltenen SHA256-Werten geprüft.
Der Build erzeugt eine Windows-Test-DLL und fünf macOS-Objektdateien.
Er erzeugt keine installierbare oder signierte Grafik-Kext.

Der [native Build](docs/INSTALLER.md#echte-diagnose-kext-bauen) ist als Skript und
GitHub-macOS-Workflow vorbereitet. Er soll eine echte Diagnose-Kext und das
Installer-Archiv erzeugen; der erste erfolgreiche Linkschritt ist noch nicht
bestätigt. Die Kext implementiert weiterhin keine GPU-Ausführung.

Der [Installer](docs/INSTALLER.md) nimmt eine ausdrücklich ausgewählte OpenCore-EFI
und bietet Planung, Installation und Rücknahme. Er ändert weder Bootreihenfolge
noch Firmware-Schlüssel oder Secure-Boot-Einstellungen. Vault-geschützte Änderungen
und `DisableSecurityPolicy=true` werden abgewiesen. Der Standard bleibt deaktivierte
Injektion und gesperrter Registerzugriff; Aktivierung benötigt explizite Optionen.

[Diagnoseanleitung](docs/DIAGNOSTICS.md) und [nativer Anschluss](docs/NATIVE_BACKEND.md)
beschreiben Snapshot, Ereignisse, CPU-Speichertest und Fehlerauswertung. Ein
gescheiterter Speichertest verwirft die zuvor erfassten Diagnosen nicht.

## Lizenz

Neue Projektarbeit steht unter **GNU GPL Version 3 ausschließlich**
(`GPL-3.0-only`). Kommerzielle Nutzung und bezahlter Support sind erlaubt.
Bei der Weitergabe abgeleiteter Versionen müssen die GPL-Pflichten einschließlich
des entsprechenden Quellcodes erfüllt werden. Andere dürfen ebenfalls
GPL-konforme Kopien weitergeben und verkaufen.

Die MIT-Hinweise von Intel und die GPL-Bedingungen von VoodooI2C bleiben erhalten.
Fremde Quelltexte, SDK-Header und Firmware erhalten dadurch keine neue Lizenz.
NootedGreen-Code wird aufgrund seiner abweichenden Lizenz nicht übernommen.
Siehe [vollständige Lizenz](LICENSE), [Herkunftshinweise](NOTICE) und
[Lizenzumfang und kommerzielle Nutzung](docs/LICENSING.md).
