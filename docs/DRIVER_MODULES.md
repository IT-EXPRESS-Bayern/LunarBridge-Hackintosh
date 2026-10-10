# Getrennte Hardware-Treiber

LunarBridge.kext ist ausschließlich für Grafik zuständig. Andere Hardware erhält
eigene Kexts oder getrennte ACPI-/Konfigurationspatches. Jede Hardware-Komponente
hat eigene Gerätezuordnung, Buildausgabe, Diagnose und Rücknahme. Eine gemeinsame
Downloadsammlung darf mehrere eigenständige Pakete enthalten; sie führt deren
Kernelcode und Gerätezuordnung nicht in einem Treiber zusammen.

Jedes größere Modul erhält außerdem ein eigenes öffentliches Git-Repository
mit eigenen Quellen, Abhängigkeiten, Builds und Prüfberichten.

| Modul | Repository |
| --- | --- |
| Grafik und Grafik-Installer | [LunarBridge-Hackintosh](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh) |
| I2C, GPIO und HID | [LunarBridge-I2C](https://github.com/IT-EXPRESS-Bayern/LunarBridge-I2C) |
| HDR und Samsung-Firmwarefunktionen | [LunarBridge-HDR-Samsung](https://github.com/IT-EXPRESS-Bayern/LunarBridge-HDR-Samsung) |
| Native Softwarekompatibilität | [LunarBridge-Native-Compatibility](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Native-Compatibility) |
| Windows-Programme unter macOS | [LunarBridge-Windows-Compatibility](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Windows-Compatibility) |
| IPU7 / OV02E10 Kamera | [LunarBridge-Camera](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Camera) |
| OpenCore, Bootauswahl und Vertrauenskette | [LunarBridge-OpenCore](https://github.com/IT-EXPRESS-Bayern/LunarBridge-OpenCore) |

| Komponente | Eigene Paketgrenze | Aktueller Stand |
| --- | --- | --- |
| Arc 130V | LunarBridge.kext | Gebaute Diagnose-Kext; GPU-Ausführung und Metal fehlen |
| I2C-Controller | VoodooI2C-Quellpatch / eigenständige VoodooI2C.kext | Separate native CI erfolgreich; Controllerfunktion ungeprüft |
| GPIO-/Interrupttransport | Eigenständige VoodooGPIO.kext | Gebaut, aber INTC105D/105F und dynamische Communities noch nicht portiert |
| Touchpad und Touchscreen | VoodooI2CHID.kext mit getrennten Geräteinstanzen | HID-over-I2C-Kandidaten; native Geräteprobe fehlt |
| BE201-WLAN | Eigenständige WLAN-Kext bzw. separater Firmware-/Familienport | Kein funktionierender BE201-Port vorhanden |
| Bluetooth | Eigenständige IntelBluetoothFirmware-/Patcher-Kexts | Gen3-/TLV- und Firmwareprüfung offen |
| SST-Audio / Codec / Verstärker | Separates Audio-Paket; Controller- und Verstärkertreiber getrennt | SST-Transport und Tahoe-Audiopfad fehlen |
| PS/2-Tastatur | VoodooPS2Controller.kext und zugehörige Tastaturkomponente | Kandidat; Funktionstasten und Standby ungeprüft |
| USB-Portzuordnung | USBToolBox.kext und eigenes Mapping | Portmapping ist kein Ersatz für einen Controller-Treiber |
| USB4 bzw. zusätzlicher Controller-Port | Eigenständiger Controller-Treiber, falls nötig | Keine native Qualifikation |
| CPU / Energie / SMC / Boot | Eigene ACPI-Dateien, Bootkonfiguration und passende separate Kexts | Gehören nicht in die Grafik-Kext |
| Samsung-Kamera | Eigener Sensor-/IPU7-/ISP-/CMIO-Pfad | Windows-Gerätebindung erkannt; native Metadatenprüfung gebaut, Aufnahmetreiber fehlt |
| HDR / Samsung-Funktionen | Eigenes Userland-Modul und später getrennte Firmware-Anbindung | Readback und begrenzte Protokollformate; keine HDR-Ausgabe oder Firmwareaufrufe qualifiziert |

Die Namen beschreiben Paketgrenzen. Sie behaupten keine Unterstützung noch
unportierter Hardware. Gemeinsame Abhängigkeiten wie Lilu oder eine I2C-
Transportbibliothek bleiben nachvollziehbare Abhängigkeiten; sie geben einem
Grafiktreiber keine Zuständigkeit für WLAN, Audio oder Eingabegeräte.

Der Grafik-Installer akzeptiert genau die Arc-Diagnose-Personality. Weitere
Hardware-Personalities, eingebettete Kexts und Dateien unter `Contents/PlugIns/`
werden vor einer EFI-Änderung abgewiesen. Andere Treiber brauchen einen eigenen
Installationsplan samt Abhängigkeiten, Reihenfolge und Rücknahme. Der aktuelle
Installer installiert ausschließlich LunarBridge.kext.

Interne Arbeitslisten und lokale Gerätelogs werden nicht als Projektdokumentation
oder Buildbestandteil veröffentlicht. Der öffentliche Prüfstand steht in
[VALIDATION.md](VALIDATION.md), die Gerätelage in [HARDWARE_SUPPORT.md](HARDWARE_SUPPORT.md).
