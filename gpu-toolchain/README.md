# Eigener Arc-130V-Compilerbaustein

Ein eigener OpenCL-Integer-Kernel wurde auf der **Intel Arc 130V dieses Samsung unter Windows** kompiliert und genau einmal mit 64 Elementen ausgeführt. Alle 64 Ergebnisse stimmen mit der unabhängig berechneten Referenz überein. Die erzeugten kleinen Dateien liefern einen konkreten Shaderbaustein für die weitere native Xe2-Treiberentwicklung.

**Hardware-Nachweis: Windows ja; native macOS-/Metal-Beschleunigung nein.** Der Baustein implementiert keinen macOS-Loader, keine GPU-Speicherverwaltung, keine Command-Submission, keinen Displaypfad und keine Metal-API. Ein lauffähiger Windows-Kernel beweist keinen funktionierenden macOS-Treiber.

## Tatsächlicher Stand

| Eigenschaft | Beobachtetes Ergebnis |
| --- | --- |
| GPU-Auswahl | Intel Arc 130V, Vendor `0x8086`, Geräte-ID `0x64a0`; genau ein qualifiziertes Gerät |
| Installierter Treiber | `32.0.101.9033`, Intel OpenCL 3.0 NEO, OpenCL C 1.2 |
| Buildoption | `-cl-std=CL1.2` |
| Eigener Programmbinary | 5.728 Byte, ELF64 Little Endian, `ET_REL=1`, `EM_INTELGT=205`, Zebin-Metadaten 1.73 |
| Eigener GPU-Codeabschnitt | 896 Byte; Symbolkörper 768 Byte, übrige 128 Byte bleiben unverändert enthalten |
| Compiler-Metadaten | SIMD32, 128 GRF, Inline-Payload 32 Byte; lokale IDs 64 Byte |
| Eigener SPIR-V-Abschnitt | 1.464 Byte |
| Ergebnisprüfung | 64 von 64 `uint32`-Werten identisch, ein GPU-Dispatch |
| API-Ressourcenfreigaben | Alle sieben Freigaben meldeten OpenCL-Status 0 |
| Offline-Prüfung dieses Moduls | 37 Tests bestanden; keine zusätzliche GPU-Ausführung |

Die gemeldeten 8.541 ns sind die OpenCL-Ereigniszeit dieser einzelnen kleinen Ausführung. Das ist kein allgemeiner Leistungsbenchmark. Die installierten ICD-/Compilerdateien melden FileVersion `32.0.101.9033`; daraus lässt sich ihr konkreter IGC-Upstream-Commit nicht ableiten.

## Lesende Prüfung und Extraktion

Python 3.12 oder neuer genügt; der ELF-Inspector nutzt nur die Standardbibliothek und benötigt keinen Grafiktreiber. Vom Repository-Stamm:

```text
python gpu-toolchain/inspect_own_zebin.py
python gpu-toolchain/inspect_own_zebin.py --binary gpu-toolchain/fixtures/own-xe2-program.bin
python gpu-toolchain/inspect_own_zebin.py --output-dir work/xe2-extract-neu
```

Ohne `--output-dir` liest der Inspector und gibt JSON aus. Mit `--output-dir` legt er ein neues Verzeichnis an und exportiert feste, bekannte Abschnittnamen bytegleich. Der Inspector führt keinen Binary aus, initialisiert kein OpenCL und wendet keine Relokationen an. Er begrenzt den Input auf 16 MiB, die Abschnittzahl auf 256, Namenstabellen und IntelGT-Notes auf 64 KiB sowie Symbol-/Relokationstabellen auf 1.024 Einträge. Er prüft Dateibereiche, Überlappungen, Namen, Symbole, Relokationsziele und Note-Grenzen. Das Modul behandelt den eigenen Kernel; es ist kein allgemeiner ELF-Loader.

## Ausdrücklicher GPU-Aufruf unter Windows

Das OpenCL-Skript nutzt ausschließlich den bereits installierten Systemloader. Ohne Ausführungsoption fragt es nur Plattformen und GPU-Geräte ab:

```text
python gpu-toolchain/opencl_xe2_probe.py
```

Erst dieser ausdrückliche Aufruf kompiliert und startet den eigenen Kernel einmal:

```text
python gpu-toolchain/opencl_xe2_probe.py --execute --output-dir work/xe2-run-neu
```

Das Ausgabeverzeichnis muss neu sein. Der Aufruf benötigt 64-Bit-Windows, ein verfügbares Intel-GPU-Gerät mit Vendor-ID `0x8086`, passendem Arc-130V-Namen und verfügbarem OpenCL-Compiler. Ohne genau ein solches Gerät wird die Ausführung abgewiesen. CPU-OpenCL und andere GPUs werden nicht als Ersatz verwendet. Es gibt keinen Download, keine Treiberinstallation, keine direkten MMIO-Zugriffe, keine Resets und keine Änderung von Boot- oder Sicherheitseinstellungen. DLL-Pfade, Geräte-UUIDs und Seriennummern werden nicht in die Berichte geschrieben. Ein eventuell angefallener Compiler-Buildlog bleibt im gewählten lokalen Ausgabeverzeichnis als private Datei.

Die öffentliche CLI wurde nach der Hardwareprobe für `--execute` und ein frei gewähltes neues Ausgabeverzeichnis angepasst. Die tatsächlich ausgeführte Quellversion liegt bytegleich in `fixtures/executed-probe-source.py.txt`; ihr historischer SHA ist im Hardwarebericht erhalten. **Die überarbeitete öffentliche CLI wurde nicht erneut auf der GPU ausgeführt.** Das tatsächliche Ergebnis bleibt an die ursprüngliche Probe und ihren eigenen Quellhash gebunden.

## Ansatzpunkte für den nativen Treiber

Die `.ze_info` beschreibt Eingabepointer bei Payload-Offset 24 mit 8 Byte, Ausgabepointer bei 32 mit 8 Byte und den Zähler bei 40 mit 4 Byte. Global-ID-Offset und lokale Gruppengröße liegen davor. Die IntelGT-Product-Config `0x05010004` stimmt mit der von OpenCL abgefragten Geräte-IP überein.

Zwei `R_ZE_SYM_ADDR_32`-Relokationen liegen bei Codeoffset 76 und 156. Beide verweisen auf `__INTEL_PATCH_CROSS_THREAD_OFFSET_OFF_R0`. Die ursprünglichen vier Byte sind jeweils null. Intel definiert dieses Symbol als ImplicitArgs-Relokation; der Runtime-Linker setzt es abhängig vom benötigten ImplicitArgs-Layout. Der künftige native Treiber muss das zu seinem Dispatchlayout passende Verhalten bestimmen. Ein bloßes Kopieren des Codeabschnitts löst diese Aufgabe nicht. Die Fixtures sind unverändert; der Inspector patcht nichts. Die Definition und Behandlung stehen im gepinnten [Intel-Linkerheader](https://github.com/intel/compute-runtime/blob/7323fcafa8979708e73f1865c61cb9790b367948/shared/source/compiler_interface/linker.h#L22) und [Runtime-Linker](https://github.com/intel/compute-runtime/blob/7323fcafa8979708e73f1865c61cb9790b367948/shared/source/compiler_interface/linker.cpp#L697).

Maschinencode, SPIR-V, Metadaten und Relokationstabellen sind vorhanden. Eine menschenlesbare ISA-Disassemblierung ist hier nicht enthalten. Die Intel IGA-Quellen dokumentieren das [Xe2-Modell einschließlich Lunar-Lake-Alias](https://github.com/intel/intel-graphics-compiler/blob/6f81427a0be7c7c017d5be3f049dabec393cf788/visa/iga/IGALibrary/Models/Models.cpp#L393); aus dieser Quellenprüfung wird keine ausgeführte IGA-Disassemblierung abgeleitet.

## Prüfung und Herkunft

```text
python -m unittest discover -s gpu-toolchain/tests -p test_xe2_shader.py -v
```

Die 37 Tests prüfen GPU-Auswahl, Ergebnisfehler einschließlich tatsächlich erzeugter Ergebnisbytes, unsigned Integer-Überlauf, Fehlerstatus, ELF-/Note-/Symbol-/Relokationsgrenzen und die bytegleichen Exporte. Die Tests initialisieren kein OpenCL und führen keine GPU-Programme aus. `fixtures/MANIFEST.json` hält Größen und SHA256-Werte der eigenen Fixtures fest.

Eigener Kernel, Prüfcode und die ausschließlich daraus erzeugten Fixtures stehen unter **GPL-3.0-only**, siehe die GPL-Lizenz im Repository-Stamm. Es sind keine Hersteller-DLLs oder fremden Shaderprogramme enthalten. Die verwendeten Intel-Formatdefinitionen und Primärquellen stehen unter MIT; Herkunft und Lizenzhinweis stehen in [NOTICE](NOTICE), die Quellenpins in `fixtures/PRIMARY_FORMAT_REFERENCES.json`. Die Versionspins dieser Dokumentationsquellen sind ausdrücklich keine nachgewiesenen Buildcommits des installierten Windows-Compilers.
