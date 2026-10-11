# LunarBridge-Installer

Der Installer verwaltet ausschließlich `LunarBridge.kext` und ihren Eintrag in
einer ausdrücklich ausgewählten OpenCore-EFI. Er bietet `plan`, `install` und
`rollback`. Die aktuelle Version installiert den experimentellen Diagnosetreiber.
GPU-Ausführung, Metal und Displaybeschleunigung sind darin nicht implementiert.

Unter Windows wurden 23 Prüfungen an künstlichen EFI-Verzeichnissen bestanden.
Zusätzliche Hardware-Personalities und eingebettete Kexts werden vor einer
EFI-Änderung abgewiesen; der Installer gehört ausschließlich zum Grafikmodul.
Es wurde keine reale EFI verändert und kein Treiber auf macOS geladen.
Die Windows-Builds sind Objektdateien und können nicht installiert werden.

## Echte Diagnose-Kext bauen

Auf macOS mit Xcode Command Line Tools, Python 3 und einem separat bezogenen
[MacKernelSDK](https://github.com/acidanthera/MacKernelSDK):

```sh
python3 tools/build_native.py --sdk /path/to/MacKernelSDK --out build/native
```

Der Build verwendet den macOS-Kext-Linker und eigene minimale Moduleinstiege.
Es wird keine statische SDK-Bibliothek in den Treiber übernommen.
Er prüft Mach-O-Typ, Architektur, C++-Initialisierer und Kmod-Metadaten und erzeugt
`build/native/LunarBridge.kext` sowie `LunarBridge-Installer.zip` mit Installer,
Diagnosewerkzeugen, entsprechendem Treiberquellcode, Buildskript und Lizenzhinweisen.
`SOURCE-REFERENCE.json` enthält Quellstand, Einstellungen und Quell-/Binary-Prüfsummen.
Ein erster echter macOS-Linkbuild ist im GitHub-Workflow erfolgreich durchgelaufen;
der jeweils geprüfte Quellstand steht in [VALIDATION.json](VALIDATION.json).
Die Strukturprüfung bestätigt weder Signierung noch Laden oder Tahoe-ABI-Kompatibilität.
Das externe SDK basiert auf älteren macOS-Headern; die Ziel-Tahoe-Probe bleibt notwendig.

Der Standardbuild lässt MMIO-Registerlesen und zusätzliche Kernelmeldungen aus.
`--debug-log` aktiviert die begrenzten Kernelmeldungen. `--mmio-read` aktiviert
explizit den experimentellen einmaligen GMD-Registerlesepfad; seine Vorbedingungen
stehen in [NATIVE_BACKEND.md](NATIVE_BACKEND.md). Er ist keine GPU-Initialisierung.
Der LNL-Lesepfad führt vor jedem Read vier notwendige Dummywrites aus.
`--forcewake-probe --debug-log` baut zusätzlich die ausdrücklich aktivierte
Main-GT-Wake-/Releaseprobe. Der Plan zeigt effektive MMIO-/Flush- und
Forcewake-Einstellungen getrennt. Die [Bring-up-Beschreibung](GPU_BRINGUP.md)
nennt deren Umfang und die noch fehlende native GPU-Ausführung.

## Ziel prüfen und planen

`--efi-root` bezeichnet den Ordner mit `OC/config.plist`, beispielsweise
`/Volumes/EFI/EFI`. Das Einhängen und die Auswahl der richtigen externen EFI
erfolgen außerhalb dieses Installers. Er sucht keine Laufwerke selbst und
partitioniert oder formatiert keine SSD.

```sh
python3 installer/install_opencore.py plan \
  --efi-root /Volumes/EFI/EFI --kext build/native/LunarBridge.kext
```

`plan` schreibt keine Dateien. Es zeigt Prüfsummen, MMIO-Einstellung und die
geplante Aktivierung. Ohne `--enable` bleibt der Treiber in `Kernel/Add` deaktiviert.
Mit `--enable` wird die Injektion beim nächsten Tahoe-Start vorgesehen.
Der Eintrag gilt nur für Darwin 25 (`25.0.0` bis `25.99.99`).

Die Quelle muss zur engen Samsung-Arc-130V-Personality passen und ein x86_64
`MH_KEXT_BUNDLE` mit passenden Kmod-Metadaten enthalten. Eine umbenannte `.o`-Datei,
abweichende Architektur, fremde Bundle-Identität oder veränderte Gerätezuordnung
wird abgewiesen. Diese Prüfung ersetzt keine native Ladeprüfung.

## Installieren und zurücknehmen

Nach realer Build-/EFI-Qualifikation:

```sh
python3 installer/install_opencore.py install \
  --efi-root /Volumes/EFI/EFI --kext build/native/LunarBridge.kext --enable
```

Der Installer sichert die vorherigen Konfigurationsbytes und gegebenenfalls die
alte LunarBridge-Kext unter `OC/LunarBridge-Backups/<Zeitpunkt-ID>/`. Er prüft die
kopierten Dateien und erkennt Änderungen während der Vorbereitung. Andere
Kext-Einträge und OpenCore-Einstellungen werden semantisch erhalten; die aktive
Plist wird neu serialisiert. Die Sicherung enthält ihre ursprünglichen Bytes.

Die ausgegebene `install-receipt.json` gehört genau zu dieser EFI. Rücknahme:

```sh
python3 installer/install_opencore.py rollback \
  --efi-root /Volumes/EFI/EFI \
  --receipt /Volumes/EFI/EFI/OC/LunarBridge-Backups/<Zeitpunkt-ID>/install-receipt.json
```

Rücknahme stellt das vorherige Bundle und die ursprüngliche Konfiguration wieder
her. Spätere manuelle Änderungen an Konfiguration oder installierter Kext werden
erkannt und verhindern deren Überschreiben. Fehler während Kopieren, Konfigurations-
oder Receipt-Schreiben werden abgefangen und die getesteten Abbruchpfade nehmen
bereits erfolgte Änderungen zurück. Ein Stromausfall oder weitere Fehler während
der Wiederherstellung sind nicht durch diese Transaktion abgesichert. Ein Receipt
mit `interrupted` benötigt Abgleich mit dem erhaltenen Backup; es wird nicht als
erfolgreich installierter oder automatisch rücknehmbarer Stand behandelt.

Backups und Receipts bleiben privat: `config.plist` kann SMBIOS-Identitäten und
andere persönliche Einstellungen enthalten. Sie gehören nicht ins öffentliche Repo.

## Secure Boot und externe SSD

Firmware-UEFI-Secure-Boot bleibt eine Abnahmebedingung. Der Installer ändert keine
Firmware-Schlüssel, Bootreihenfolge, Windows-Dateien, `OpenCore.efi`, SIP-Einstellung
oder `SecureBootModel`. Er verweigert `DisableSecurityPolicy=true`. Eine aktivierte
OpenCore-Vault oder vorhandene Vault-Dateien werden abgewiesen, weil jede Änderung
eine neue gültige Vault-Signatur benötigt. Vault wird nicht automatisch ausgeschaltet.

Apples `SecureBootModel` bestätigt keine Firmware-UEFI-Vertrauenskette. Die gültige
UEFI-Signierung und Schlüsselannahme des Bootloaders sowie Kext-Injektion und beide
Startwege müssen mit der Samsung-Firmware geprüft werden. Die externe Lexar-SSD
darf erst nach bestätigter Datensicherung neu aufgeteilt werden. Diese Geräteprüfungen
sind weiterhin offen; der [Prüfbericht](VALIDATION.md) dokumentiert den belegten Stand.
