# LunarBridge-Hackintosh

Experimentelle Entwicklung nativer macOS-Treiber für Intel Lunar Lake.
Der geprüfte Entwicklungsstand steht in [VALIDATION.md](docs/VALIDATION.md).
Das erste Ziel ist die Intel Arc 130V (`8086:64A0`) im Samsung Galaxy Book5 Pro.
Das Projekt enthält eigene native Diagnose-/CPU-Speicherbausteine, einen
Installer und Tests unter Windows. Eine echte native Diagnose-Kext ist gebaut.
**GPU-Ausführung, Arc-Metal-Beschleunigung und eine qualifizierte bootfähige EFI
liegen noch nicht vor.**

[Geprüfter Stand](docs/VALIDATION.md): **250 bestandene Kernprüfungen unter Windows**,
13 erfolgreiche Compiler-Aufrufe und zwölf macOS-Objektdateien.
Der GitHub-macOS-Build hat eine echte Diagnose-Kext gelinkt.
**Keine bestandene native macOS-Geräteprobe**.

Der [native Main-GT-Aufweckadapter](docs/GPU_BRINGUP.md) ist jetzt implementiert,
mit dem Lunar-Lake-Registerworkaround und gehaltenen Fehlerressourcen.
Ein eigener [Xe2-Shader](gpu-toolchain/README.md) wurde auf der tatsächlichen
Arc 130V unter Windows ausgeführt: 64/64 korrekte Ergebnisse. Die native
macOS-Ausführung dieses Kernels und Metal fehlen weiterhin.

## Vorhandene Ergebnisse

| Baustein | Ergebnis | Aussage der Prüfung |
| --- | --- | --- |
| PCI-Erkennung für Lunar Lake | Eigener C++-Baustein für drei Linux-gelistete Geräte-IDs | Kompilierbar und unter Windows ausführbar |
| GMD_ID-Auswertung | Grafik-/Media-Registeradressen, Versionsfelder und Bereichsprüfung | Synthetische Registerwerte und Grenzfälle prüfbar; kein echter Registerzugriff |
| Nativer IOKit-Diagnosetreiber | Enge Samsung-/Arc-Zuordnung, PCI-Snapshot und expliziter GMD-Lesepfad mit Vorbedingungen | Quellcode als macOS-Objektdatei kompiliert; kein nativer Register-/Gerätetest |
| Nativer CPU-Speicheranschluss | Administrativer User-Client, eigene genullte Puffer und Größen-/Clientgrenzen | Quellcode kompiliert, portable Speicherregeln unter Windows ausgeführt; native Mappings ungeprüft |
| Nativer DMA-Anschluss | Exakter PCI-Mapper, begrenzte Vorbereitung, PTE-Staging und gehaltene Fehlerquarantäne | 30 Adapter- und zehn Ownership-Tests mit simuliertem IOKit; standardmäßig aus, keine Geräteprobe |
| Tracker und Traces | Fester ABI, begrenzter Ereignisring, Sammler und Fehlerbericht | C++-Ringpuffer und simulierte IOKit-/Auswertungsfälle geprüft |
| Xe2-Speichergrundlagen | Getrennte GPU-VA-Reservierungen, PTE-Encoding, Tabellenplanung und DMA-Listen im CPU-Speicher | Teil von 31 Grundlagenprüfungen; echte DMA-Bindung und GPU-Tabellen fehlen |
| GuC-Grundlagen | CSS-Headerprüfung, HXG-/CT-Nachrichten und begrenzte CPU-Schattenringe | Teil von 31 Grundlagenprüfungen; kein authentifizierter Firmwarestart oder Hardwaretransport |
| Grafik-Installer für OpenCore | Planen, sichern, installieren und zurücknehmen; Prüfsummen und Modulgrenzen | 21 Prüfungen an künstlichen EFI-Verzeichnissen; keine echte EFI geändert |
| Grafikbeschleunigung, Display und Metal | Architekturvergleich und mögliche Portierungsstellen | Noch keine native Hardwarevalidierung |

Die Windows-Treiberkomponenten wurden lokal anhand ihrer Dateimetadaten und
Import-/Exporttabellen verglichen. Ihr Quellcode wurde dadurch nicht gewonnen.
Das öffentliche Repository enthält keine Windows- oder Apple-Treiberdateien.

## Untersuchte native Grafikroute

Der untersuchte Weg verbindet eine mögliche Metal-Übersetzung, einen
Xe2-fähigen Shader-Compiler, einen neuen nativen macOS-Anschluss und portierte
Teile des Linux-Xe-Treibers. Der Diagnose-/CPU-Pufferanschluss liegt als eigener
Quellcode vor. GPU-VA-, PTE- und GuC-Formate sind inzwischen als CPU-seitige
[Grundlagen](docs/GPU_FOUNDATION.md) implementiert. Der [native DMA-Adapter](docs/DMA_BACKEND.md)
bereitet Speicher mit Geräte-Mapper vor und muss am Zielgerät geprüft werden.
GPU-Bindung, Firmware-Kommunikation, Ausführungswarteschlangen und Metal-Anbindung fehlen.
[Architektur und konkrete Quellstellen](docs/GPU_DESIGN.md)
erklären, welche Teile als Referenz dienen und welche noch fehlen.

Ein erster Versuch könnte sich auf eine Ausführungswarteschlange und begrenzte
Puffer konzentrieren. Weniger Funktionen ändern jedoch nicht die
Xe2-Befehlssprache und die Abhängigkeiten von Speicherverwaltung und Firmware.
Eine zusätzliche Geräte-ID in einem alten Xe-LP-Treiber reicht daher nicht aus.

I2C ist in das eigene [LunarBridge-I2C-Repository](https://github.com/IT-EXPRESS-Bayern/LunarBridge-I2C)
ausgegliedert. Es baut fünf getrennte Kexts; native
Touchpad-/Touchscreen-Proben bleiben offen. Der experimentelle Samsung-GPI1-Port
ist implementiert, nativ gebaut und standardmäßig deaktiviert. Alle größeren Module haben eigene
[öffentliche Repositories und Paketgrenzen](docs/DRIVER_MODULES.md), darunter
HDR/Samsung, native Software, Windows-Programme, Kamera und OpenCore.

## Ziel und Abnahme

Die Vorgaben bleiben: natives macOS Tahoe auf einer externen USB-C-SSD,
Windows auf dem internen Laufwerk und Auswahl beim Start. Firmware-UEFI-Secure-Boot
wird für den ersten nativen Versuch ausdrücklich zurückgestellt; das
[OpenCore-Testprofil](https://github.com/IT-EXPRESS-Bayern/LunarBridge-OpenCore/blob/main/docs/TEST_PROFILES.md)
erhält die strenge Variante für später. Eine VM ersetzt dieses Ziel nicht.
Apples SecureBootModel bestätigt keinen aktivierten Firmware-Secure-Boot.
Boot, Secure Boot, Arc-130V-Metal-Ausführung, internes Display und Standby sind
jeweils getrennt am echten Gerät nachzuweisen; bisher ist keiner dieser
nativen Abnahmetests bestanden.

Der eigene Metal-Funktionstest wurde inzwischen auf einem Intel-macOS-CI-Host
gebaut und ausgeführt: 65.536 Shader-Ergebnisse ohne Abweichung, bestätigter
Command-Abschluss und kein CPU-Ersatz. Weitere 18 Windows-Grenztests bestanden.
Der [Nachweis](docs/METAL_VALIDATION.json) gilt für diesen separaten CI-Host;
er bestätigt keine Arc-130V-Unterstützung oder Grafikbeschleunigung auf dem Samsung.

## Bauen und prüfen

Python 3, Zig 0.15.2 und externe MacKernelSDK-Header sind erforderlich.
Die Befehle stehen in der [englischen README](README.md#build-and-check-under-windows).
Die Referenzdateien werden mit festgehaltenen SHA256-Werten geprüft.
Der Build erzeugt eine Windows-Test-DLL und zwölf macOS-Objektdateien.
Er erzeugt keine installierbare oder signierte Grafik-Kext.

Der [native Build](docs/INSTALLER.md#echte-diagnose-kext-bauen) ist als Skript und
GitHub-macOS-Workflow verfügbar. Ein echter Diagnose-Kext-/Installer-Build ist
bestanden; Quellstand und Prüfsummen stehen in [VALIDATION.json](docs/VALIDATION.json).
Die Kext implementiert weiterhin keine GPU-Ausführung. Das Installer-Archiv
enthält auch den entsprechenden Treiberquellcode und das Buildskript.

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

Die MIT-Hinweise von Intel bleiben erhalten. Das separate I2C-Repository bewahrt
seine jeweiligen GPL- und Apple-Herkunftshinweise.
Fremde Quelltexte, SDK-Header und Firmware erhalten dadurch keine neue Lizenz.
NootedGreen-Code wird aufgrund seiner abweichenden Lizenz nicht übernommen.
Siehe [vollständige Lizenz](LICENSE), [Herkunftshinweise](NOTICE) und
[Lizenzumfang und kommerzielle Nutzung](docs/LICENSING.md).
