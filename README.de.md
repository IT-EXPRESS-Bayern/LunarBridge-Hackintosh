# LunarBridge-Hackintosh

Experimentelle Entwicklung nativer macOS-Treiber für Intel Lunar Lake.
Das erste Ziel ist die Intel Arc 130V (`8086:64A0`) im Samsung Galaxy Book5 Pro.
Das Projekt steht bei der Quellcodeanalyse, ersten eigenen Bausteinen und
Tests unter Windows. **Ein funktionierender Arc-Metal-Treiber, eine bootfähige
EFI und eine installierbare Grafik-Kext liegen noch nicht vor.**

[Geprüfter Stand](docs/VALIDATION.md): **16 bestandene Prüfungen unter Windows**,
drei erfolgreiche Compiler-Aufrufe, **keine bestandene native macOS-Geräteprobe**.

## Vorhandene Ergebnisse

| Baustein | Ergebnis | Aussage der Prüfung |
| --- | --- | --- |
| PCI-Erkennung für Lunar Lake | Eigener C++-Baustein für drei Linux-gelistete Geräte-IDs | Kompilierbar und unter Windows ausführbar |
| GMD_ID-Auswertung | Grafik-/Media-Registeradressen, Versionsfelder und Bereichsprüfung | Synthetische Registerwerte und Grenzfälle prüfbar; kein echter Registerzugriff |
| IOKit-Anschluss | Funktion zum Lesen von PCI-Konfigurationswerten | Als macOS-Objektdatei kompilierbar; kein geladener Treiber |
| Samsung-I2C | Quellpatch für vier Controller mit Samsung-Subsystemfilter und Linux-basierten Timings | Konfiguration und Gerätezuordnung prüfbar; keine native Geräteprobe |
| Grafikbeschleunigung, Display und Metal | Architekturvergleich und mögliche Portierungsstellen | Noch keine native Hardwarevalidierung |

Die Windows-Treiberkomponenten wurden lokal anhand ihrer Dateimetadaten und
Import-/Exporttabellen verglichen. Ihr Quellcode wurde dadurch nicht gewonnen.
Das öffentliche Repository enthält keine Windows- oder Apple-Treiberdateien.

## Untersuchte native Grafikroute

Der untersuchte Weg verbindet eine mögliche Metal-Übersetzung, einen
Xe2-fähigen Shader-Compiler, einen neuen nativen macOS-Anschluss und portierte
Teile des Linux-Xe-Treibers. Die Verbindung dieser Bausteine ist noch nicht
implementiert. [Architektur und konkrete Quellstellen](docs/GPU_DESIGN.md)
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
Der Build erzeugt eine Windows-Test-DLL und zwei macOS-Objektdateien.
Er erzeugt keine installierbare oder signierte Grafik-Kext.

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
