# Tatsächliche IOAccelerator-Aufrufe aus dem Recovery-Abbild

Die Offline-Untersuchung liefert konkrete Aufrufstellen zwischen Apples
IOAccelerator-Framework und IOKit. **216 exportierte Funktionen wurden dekodiert,
78 IOKit-Aufrufstellen gefunden.** Das hilft beim Entwurf eigener User-Client-
Verträge. Ein funktionierender Metal-Provider oder eine native Xe2-Ausführung
ist dadurch noch nicht nachgewiesen.

Die Eingabe stammt aus dem bereits lokal vorhandenen Recovery-System
macOS 26.6.2 / 25G83. Das ist der untersuchte Stand, keine Aussage zur neuesten
verfügbaren macOS-Version. Das IOAccelerator-Framework hat Version 487.4.3.
Die Originaldateien und die gesamte frühere Metadatenanalyse sind in
[RECOVERY_GRAPHICS.md](RECOVERY_GRAPHICS.md) beschrieben.

## Gefundene Verbindungstypen und Datenlängen

Die folgenden Zahlen sind in den tatsächlichen x86_64-Aufrufstellen beobachtet.
Ausgabegrößen sind die **angeforderten Kapazitäten vor dem Aufruf**, nicht
beobachtete Rückgabewerte eines laufenden Kerneltreibers. Eine Befehlsnummer
gehört immer zum jeweiligen User Client; gleiche Nummern in verschiedenen
Verbindungen haben unterschiedliche Bedeutungen.

| Bereich / Funktion | Beobachteter Aufruf | Eingabe | Ausgabe-Kapazität |
| --- | --- | --- | --- |
| DeviceCreateWithAPIProperty | IOServiceOpen, Typ 5 | Registry-Service und Task | Verbindungsport |
| SharedCreate | IOServiceOpen, Typ 6 | Registry-Service und Task | Verbindungsport |
| CommandQueueCreateWithQoS | IOServiceOpen, Typ 8 | Registry-Service und Task | Verbindungsport |
| DeviceCreateWithAPIProperty, Device-Information | IOConnectCallStructMethod, Befehl 2 | 0 Bytes | 600 Bytes |
| DeviceCreateWithAPIProperty, weiterer Informationsblock | IOConnectCallStructMethod, Befehl 0 | 0 Bytes | 64 Bytes |
| SharedCreate, gemeinsamer Informationsblock | IOConnectCallStructMethod, Befehl 9 | 0 Bytes | 16 Bytes |
| SharedAllocateFenceMemory | IOConnectCallStructMethod, Befehl 12 | 8 Bytes | 8 Bytes |
| CommandQueueCreateWithQoS, Konfiguration | IOConnectCallStructMethod, Befehl 5 | 1028 Bytes | 8 Bytes |
| CommandQueueSubmitCommandBuffers | IOConnectCallMethod, Befehl 1 | 0 skalare Eingaben, Struktur mit 8 + 24 × N Bytes | keine Ausgaben in dieser Aufrufstelle |
| CommandQueueSetPriorityAndBackground | IOConnectCallMethod, Befehl 2 | 0 skalare Eingaben, 12 Strukturbytes | keine Ausgaben in dieser Aufrufstelle |
| CommandQueueSetQualityOfService | IOConnectCallMethod, Befehl 4 | 0 skalare Eingaben, 4 Strukturbytes | keine Ausgaben in dieser Aufrufstelle |

Der Submission-Wrapper liest N als vorzeichenbehafteten 32-Bit-Wert bei
Byteoffset 4 seines Eingabeblocks. Zwei beobachtete Adressberechnungen erzeugen
3 × N und anschließend 8 + 8 × (3 × N). Daraus folgen ein 8-Byte-Header und
24 Bytes je Eintrag. **Die Bedeutungen der übrigen Header- und Eintragsfelder
sind noch offen.** Negative Werte, Integerüberläufe und unbekannte Handles
dürfen bei einer späteren eigenen Implementierung nicht akzeptiert werden.

Die Queue-Konfiguration übergibt einen 1028-Byte-Block; dessen letzte vier Bytes
werden aus dem zweiten Argument des Wrappers gefüllt. Die Interpretation des
vorhergehenden 1024-Byte-Bereichs ist damit nicht vollständig geklärt. Auch ein
8-Byte-Fence-Ergebnis ist noch kein qualifizierter GPU- oder CPU-Zeiger.

## Wie der Befund geprüft wurde

Das eigene GPLv3-Werkzeug [ioaccel_calls.py](../tools/ioaccel_calls.py) benutzt
den vorhandenen read-only Mach-O-/dyld-Parser und Capstone 5.0.9. Es prüft:

- Zuordnung von Hauptcache und Subcache sowie ihre VM-Mappings;
- Funktionsgrenzen aus LC_FUNCTION_STARTS und Zugehörigkeit zu __text;
- nur kurze indirekte Sprung-Stubs innerhalb der eigenen __stubs-Sektion;
- die tatsächliche Mitgliedschaft einer Pointer-Adresse in einer Slide-v2-
  Rebase-Kette, bevor deren Ziel dekodiert wird;
- Zuordnung dieses Ziels zu einem tatsächlich exportierten IOKit-Symbol;
- Original-Hashes beider Eingabedateien vor und nach der Analyse.

Die automatische Argumentauswertung meldet nur lokal belegte Konstanten des
vorangehenden geraden Instruktionsblocks. Nach Sprüngen, Aufrufen, unbekannten
Speicherzugriffen und Teilregisteränderungen bleibt eine nicht belegte Größe
unbekannt (`null`). Sie ersetzt keine vollständige Kontrollflussanalyse.
Lokale Kapazitätszellen geben Größenwerte, keine nutzbaren Laufzeitadressen.
Nachgestellte, nicht dekodierbare Nullauffüllung bis 15 Bytes wird gesondert
gezählt; andere Dekodierungsabbrüche führen zu einem Fehler.

[IOACCEL_CALLS.json](IOACCEL_CALLS.json) enthält die 78 Aufrufstellen,
Original-Hashes, Aufrufadressen, lokale Konstanten und eingegrenzte Fakten.
Es enthält keine Apple-Instruktionskörper oder Treiber-Binärdateien.
Die [24 synthetischen Tests](../tests/test_ioaccel_calls.py) prüfen gültige und
ungültige Rebase-Ketten, falsche Zieladressen, Alias- und Teilregisteränderungen,
Kontrollflussgrenzen und Kapazitätszellen. Sie testen keine reale GPU.

## Selbst reproduzieren

Voraussetzung sind die beiden unveränderten lokal extrahierten Dateien
`dyld_shared_cache_x86_64` und `dyld_shared_cache_x86_64.01` im selben Ordner.
Der übrige Apple-Dateibaum ist für diese zusätzliche Untersuchung nicht nötig.
Die gehashten Abhängigkeiten unterstützen Windows/Linux x86_64. Eine separate
virtuelle Python-Umgebung vermeidet Änderungen an anderen Python-Projekten.

```text
python -m venv build/ioaccel-env
```

Unter Windows:

```text
build\ioaccel-env\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r tools/ioaccel-requirements.txt
build\ioaccel-env\Scripts\python.exe -B tests/test_ioaccel_calls.py
build\ioaccel-env\Scripts\python.exe -B tools/ioaccel_calls.py --dyld-directory "D:\lokale-recovery-dateien\dyld" --report build/ioaccel-calls.json
```

Unter Linux den Interpreter `build/ioaccel-env/bin/python` verwenden. Die
Ausgabedatei muss außerhalb des Eingabeordners liegen. Eine andere Cache-
Architektur oder ein anderes Rebase-Format wird nicht automatisch angenommen.
Bei anderen macOS-Builds müssen alle Befunde erneut erhoben werden.

## Was wir jetzt implementieren können und was noch fehlt

Die gefundenen Nummern und Datenlängen erlauben gezielte Dispatch- und
Validierungsentwürfe für Device, Shared Memory und Command Queue. Der nächste
Schritt ist der Abgleich mit den passenden Kernel-User-Clients und deren
Methodentabellen: Objektbesitz, Handleauflösung, Speicher-Mappings, Fehlercodes
und Benachrichtigungen müssen zu denselben Verbindungen passen.

Danach braucht die native Xe2-Seite weiterhin initialisierte GPU-/GuC-Firmware,
eigene GPU-Adressräume und Ressourcen, Command-Buffer-Übersetzung, funktionierende
Fence- und Completion-Signale sowie Shader-Code im passenden Xe2-Format.
Die Verknüpfung mit Metal und die tatsächliche Bildausgabe über WindowServer
sind eigene verbleibende Integrationsschritte. Ein erfolgreicher IOKit-Aufruf
allein darf nicht als ausgeführte GPU-Arbeit quittiert werden.

Apples öffentliche [IOKitLib.h](https://github.com/apple-oss-distributions/IOKitUser/blob/323ead896d04424f87184d8f6ff0cce811aab106/IOKitLib.h)
klärt die generischen Parametertypen von IOServiceOpen und IOConnectCallMethod.
Der öffentliche [Surface-Control-Code](https://github.com/apple-oss-distributions/IOKitUser/blob/323ead896d04424f87184d8f6ff0cce811aab106/graphics.subproj/IOAccelSurfaceControl.c)
ist eine weitere Referenz für ältere Surface-Aufrufe, ersetzt aber diese
buildspezifische Command-Queue-Untersuchung nicht. Die
[dyld-Formatdefinition](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/include/mach-o/dyld_cache_format.h)
beschreibt das verwendete Rebase-Format. Diese Referenzen behalten ihre
Upstream-Lizenzen; ihr Implementierungscode wurde nicht in das GPL-Werkzeug
übernommen. Capstone behält ebenfalls seine eigene Lizenz.
