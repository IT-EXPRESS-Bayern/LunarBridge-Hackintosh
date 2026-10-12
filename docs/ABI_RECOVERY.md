<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Das private macOS-ABI erheben — Runbook

Stand: 12. Oktober 2026. Dieses Dokument sagt, was du **auf deinem Mac**
ausführst, damit dieses Projekt nicht mehr raten muss, und was jeder Lauf
liefert. Die Werkzeuge sind read-only, schreiben nur in die angegebene
Ausgabedatei und geben Namen, Zahlen, Offsets und Hashes aus — **kein Byte** aus
einem analysierten Binary. Das erzwingt
[`tools/abi_policy.py`](../tools/abi_policy.py) mechanisch, nicht durch Disziplin.

## Warum es überhaupt Erhebung braucht

Beschleunigung ist auf macOS ein **Paar**: ein Kerneltreiber besitzt die
Hardware und veröffentlicht einen User-Client; ein Metal-Treiber im Userspace
öffnet diesen Client und reicht Arbeit hinein. Keine der beiden Seiten ist
dokumentiert, und Apple liefert für Xe2 überhaupt keinen Treiber — es gibt also
nichts zu übernehmen, nur eine Schnittstelle zu erfüllen. Die Hardwareseite
liefert der [Linux-Port](ARC_PORT_FROM_LINUX.md). Was fehlt, ist die
macOS-Seite, und die steht nur in den installierten Binaries und im laufenden
System.

Rechtlicher Rahmen und die vier Grenzen, die dieses Projekt zieht, stehen in
[APPLE_DRIVER_ANALYSIS.md](APPLE_DRIVER_ANALYSIS.md): § 69e UrhG, Art. 6 der
EU-Softwarerichtlinie, 17 U.S.C. § 1201(f) — Analyse zur Herstellung von
Interoperabilität durch den rechtmäßigen Nutzer, beschränkt auf die dafür
notwendigen Teile.

## Die veröffentlichte Grundwahrheit — steht schon fest

Bevor irgendetwas erhoben wird, ist der Rahmen aus **gepinnter Apple-Quelle**
geparst, nicht geraten ([`tools/abi_contract.py`](../tools/abi_contract.py),
58 Pins, jede Zeile zitiert):

| Fakt | Wert | Quelle |
| --- | --- | --- |
| `IOExternalMethodDispatch2022` je Eintrag | **40 Bytes**, Füllbytes bei +25…+31 | `IOUserClient.h:168` |
| `IOExternalMethodDispatch` je Eintrag | 24 Bytes | `IOUserClient.h:160` |
| Variable-Größen-Sentinel | `0xffffffff` | `IOUserClient.h:63` |
| Skalar-Obergrenze | 16 | `IOUserClient.h:117` |
| Prüfreihenfolge | Selektor-Bereich → 4 Formprüfungen → Async → Entitlement → Handler | `IOUserClient.cpp:6607` |
| `OSMetaClass`-Record | 40 Bytes, `classSize` bei **+32** | `OSMetaClass.h:1052` |

Daraus folgt das **Orakel**, das die halbe Erhebung ohne Disassemblat möglich
macht: das Entitlement wird **nach** allen vier Formprüfungen geprüft. Ein
`kIOReturnNotPrivileged` (`0xe00002c1`) beweist also, dass Selektor **und**
Argumentform exakt stimmen — **ohne dass der Handler läuft**. Ebenso beweist
`kIOReturnNoCompletion` (`0xe00002ea`) einen Eintrag mit Null-Funktionszeiger
und `kIOReturnUnsupported` (`0xe00002c7`), dass der Client gar keine
2022-Tabelle benutzt.

## Was du ausführst

### 1. Plattform und Startfähigkeiten

```sh
python3 tools/collect_platform.py --out build/recon/platform.json \
    --efi /Volumes/EFI          # optional, wenn die ESP gemountet ist
```

Liefert: macOS-Build und Board-ID, **SIP-Wort entschlüsselt gegen die gepinnte
`csr.h`** (welche der drei Dinge erlaubt sind: unsignierte Kexte laden,
unrestricted dtrace, task_for_pid), boot-args, die GPU im PCI-Baum mit Identität,
BARs und injizierten Properties, welche Grafikfamilien überhaupt leben, die
geladenen Grafik-Kexte, die Kernel-Collections mit Hash, die Displays — und
daraus abgeleitet eine Liste **was diese Maschine zulässt und was vorher
geändert werden muss**. Ohne root läuft fast alles; wo nicht, steht es im
Datensatz. Serien, MLB, ROM und UUID werden nicht gelesen.

### 2. Die gesamte Grafik-Software inventarisieren

```sh
python3 tools/extract_macos_graphics.py --out build/recon/graphics-inventory.json
```

Geht `/System/Library/Extensions`, `DriverExtensions`, `Frameworks`,
`PrivateFrameworks` und die PlugIns darin durch und kartiert **beide Hälften**:
Identität, Version, verlinkte private Familien (`OSBundleLibraries`),
`IOKitPersonalities` mit Providerklasse und **Userclient-Klasse**, definierte
Klassen, referenzierte fremde Metaklassen, überschriebene IOKit-Methoden, die
Objective-C-Oberfläche und **welche IOKit-Einsprungpunkte** ein Userspace-Bundle
importiert (`IOServiceOpen`, `IOConnectCallStructMethod`, `IOConnectMapMemory64`
…). Dann **paart** es Kerneltreiber mit dem Metal-Bundle, das ihn fährt — diese
Paarung trägt den Submission-Vertrag.

`nm` muss im Pfad sein (Xcode Command Line Tools), sonst kommen nur
Bundle-Metadaten und das Werkzeug sagt das.

### 3. Die Selektor-Tabellen rekonstruieren

```sh
# Was steckt in der Boot-Collection?
python3 tools/abi_dispatch.py --binary \
    /System/Library/KernelCollections/BootKernelExtensions.kc --list-bundles

# Die Tabellen eines Treibers daraus
python3 tools/abi_dispatch.py \
    --binary /System/Library/KernelCollections/BootKernelExtensions.kc \
    --bundle com.apple.driver.AppleIntelICLGraphics \
    --out build/recon/dispatch-icl.json

# Oder, wenn das Bundle ein eigenes Binary auf der Platte hat
python3 tools/abi_dispatch.py \
    --binary /System/Library/Extensions/AppleIntelICLGraphics.kext/Contents/MacOS/AppleIntelICLGraphics \
    --out build/recon/dispatch-icl.json
```

Eine Kernel Collection ist ein `MH_FILESET` — **nichts muss entpackt werden**:
jeder `LC_FILESET_ENTRY` nennt Bundle-ID und Dateioffset, und
[`tools/abi_macho.py`](../tools/abi_macho.py) öffnet das Bild an dieser Stelle.
Beide Offset-Konventionen werden gegen das Puffer-Layout geprüft statt geraten.

Der Scanner findet in `__DATA_CONST,__const` Läufe gültiger Einträge und gibt je
Selektor aus: die vier Prüfwerte (mit Markierung „variabel"), `allowAsync`, den
**Entitlement-String**, die Handler-Adresse und — wenn Symbole vorhanden sind —
deren Namen. Dazu je Tabelle eine Konfidenz mit Begründung und ein
**Probe-Plan**, der sagt, welcher Selektor sich nebenwirkungsfrei bestätigen
lässt (die entitlement-gesicherten) und welcher nicht.

Drei Dinge hält der Scanner nach der adversarialen Gegenprüfung ausdrücklich
auseinander:

1. **Array-Index ≠ Wire-Selektor.** Ausgegeben wird `array_index`. Welche Nummer
   ein Client übergibt, kann um eine Basis verschoben sein, die die Subklasse
   vorher abzieht — das zeigt erst die Call-Site. `wire_selector` bleibt daher
   `null` und die Tabelle trägt `selector_base: unknown`.
2. **Prüfwerte sind exakte Anforderungen**, keine Obergrenzen: xnu vergleicht auf
   Gleichheit. Nur `0xffffffff` akzeptiert jede Größe.
3. **Null-Padding ist eine Compiler-Gewohnheit, keine ABI-Garantie.** Es verwirft
   keine Tabelle mehr, sondern senkt die Konfidenz und steht je Eintrag als
   `padding_nonzero` im Datensatz. Ebenso wird die Zeigerform (klassische
   Relokationen, rebasierte Adressen, Chained Fixups) **erkannt und benannt**,
   nicht angenommen.

Der **einzige** wörtliche String aus dem analysierten Binary, der in die Ausgabe
gelangt, ist ein Entitlement-Bezeichner — und nur, wenn er wie einer aussieht
(Reverse-DNS, höchstens 128 Zeichen). Ohne ihn kann ein eigener Treiber dieselbe
Berechtigung nicht deklarieren, also ist er genau der Fall, den die
Interoperabilitätsausnahme nennt. Alles andere an diesem Zeiger wird nur mit
Länge vermerkt.

Alle Grenzwerte kommen aus dem Vertrag oben; es gibt keine Magic Numbers im
Scanner. Belegt ist das Verfahren durch
[`tests/test_abi_dispatch.py`](../tests/test_abi_dispatch.py): 24 Tests gegen
**selbst gebaute echte Mach-O-Bilder** mit gepflanzter Tabelle — inklusive
gestripptem Binary, Legacy-Struktur und Kernel-Collection. Ein Apple-Binary
beweist das nicht, weil dort die erwartete Antwort unbekannt ist.

### 3b. Die Call-Site lesen — Eintragszahl und Selektor-Basis

```sh
# Beides in einem Lauf: Tabelle scannen und mit der Call-Site verknüpfen
python3 tools/abi_dispatch.py \
    --binary /System/Library/KernelCollections/BootKernelExtensions.kc \
    --bundle com.apple.driver.AppleIntelICLGraphics \
    --with-callsite --out build/recon/dispatch-icl.json

# Oder nur die Call-Site
python3 tools/abi_callsite.py --binary <binary> --out build/recon/callsite-icl.json
```

Gelesen wird **der `externalMethod`-Override der Subklasse** — nicht
`IOUserClient2022::dispatchExternalMethod`, die im Kernel liegt und im Treiber
nur ein importiertes Symbol ist. Dort stehen:

* die **Eintragszahl**, aus zwei unabhängigen Quellen: dem Immediate des
  Grenzvergleichs und dem `dispatchArrayCount`-Argument;
* die **Selektor-Basis**, ebenfalls doppelt: aus dem Abzug auf dem
  Selektorregister und aus dem „reject-if-below"-Vergleich;
* das **Array-Symbol**, das der Override lädt;
* welcher Dispatch-Einsprungpunkt überhaupt gerufen wird.

Entschieden wird nie aus dem Mnemonic allein — clang compiliert
`if (selector >= 11)` zu `cmpl $0xa` plus einem Sprung **in** den Rumpf, also
liefert jeder Vergleich zwei mögliche Grenzen und erst die **Übereinstimmung**
mit dem Argument entscheidet. Kleine Basen erscheinen als `addl $-0x2a`, große
als `addl $0xffffff00`; beide Formen werden gelesen.

Die Verknüpfung füllt dann `wire_selector = array_index + base` und vergleicht
die Längen. Das entscheidet nebenbei etwas, was die Bytes allein nicht
entscheiden können: dieselben 120 Bytes lassen sich als drei 2022-Einträge
**oder** als kürzerer Legacy-Lauf lesen — die Call-Site sagt „drei", und die
falsche Lesung wird als widersprüchlich markiert statt als gleichwertige
Tabelle geführt.

Disassembliert wird dafür mit `llvm-objdump`/`otool`; die Ausgabe wird geparst
und **verworfen**. In den Datensatz gelangen Zahlen, Offsets, Registernamen und
Symbolnamen — kein Instruktionstext. `tests/test_abi_callsite.py` prüft genau
das mit, gegen compilierte Fixtures mit bekannter Antwort.

### 4. Einen einzelnen Treiber kartieren

```sh
python3 tools/analyze_apple_driver.py \
    --kext /System/Library/Extensions/AppleIntelICLGraphics.kext \
    --out build/apple/icl.json --markdown build/apple/icl.md
```

### 5. Alles zusammen prüfen

```sh
python3 tools/recon.py --out build/recon
```

22 Suiten, 58 gepinnte Quellen, 73 Upstream-Fakten mit Zeilenzitat, plus die
macOS-Stufen (`appledriver`, `system`, `device`), die hier auf dem Mac endlich
laufen.

## Was mir das bringt

| Datensatz | Woraus Treibercode wird |
| --- | --- |
| `platform.json` | ob und wie der Treiber dauerhaft lädt; welche Probes die Maschine zulässt |
| `graphics-inventory.json` | welche Familie unsere `Info.plist` deklarieren muss, welche Basisklasse, welche Methoden zu überschreiben sind, welches Metal-Bundle gegen welchen Client spricht |
| `dispatch-*.json` | die exakte Selektortabelle, die unser User-Client anbieten muss: Nummern, Argumentformen, Entitlements |
| Probe-Plan | wie sich jede dieser Formen am laufenden System bestätigen lässt, bevor wir eine Zeile dagegen schreiben |

## Zwei Fallen, die die Gegenprüfung gefunden hat

* `codesign -dvvv --extract-certificates` **schreibt** DER-Dateien (`codesign0`,
  `codesign1`, …) ins aktuelle Verzeichnis. Aus dem Repository-Wurzelverzeichnis
  ausgeführt landet damit Apple-Binärinhalt im Baum. Keines unserer Werkzeuge
  ruft das auf — und du solltest es auch nicht. `codesign -dvvv --verbose=4`
  liefert Identifier, TeamID und CDHash als Text, das genügt.
* Eine Kopie des Binaries ist unnötig (das Systemvolume ist versiegelt) und darf
  **niemals** unterhalb des Repository-Baums landen, auch nicht in `build/`.
  Unsere Werkzeuge lesen an Ort und Stelle und schreiben nur den Datensatz.

## Laufzeitbeobachtung: was zulässig ist und was nicht

Die adversariale Gegenprüfung der Laufzeit-Spezifikation hat 18 Korrekturen und
sechs Guardrail-Verstöße gefunden. Deshalb gibt es hier **kein** Sweep-Skript,
sondern erst die Regeln, die daraus folgen — und zwei davon sind im Code
erzwungen.

**Nur entitlement-geschützte Selektoren sind nebenwirkungsfrei bestätigbar.**
Für jeden anderen Eintrag führt eine *korrekte* Argumentform den Handler
**zwangsläufig** aus — das ist die Dispatch-Semantik, keine Wahrscheinlichkeit
(`IOUserClient.cpp:6658`). Im laufenden Grafiktreiber ist das unbekannter
Kernelcode: Panic, GPU-Hang oder stiller Zustandsschaden. Ein Sweep über
„Selektoren, die nichts submittieren" ist unmöglich, weil man vor dem Sweep
nicht weiß, welcher submittiert. Der Probe-Plan aus
[`tools/abi_dispatch.py`](../tools/abi_dispatch.py) markiert daher je Eintrag
`handler_runs`, und nur `handler_runs: false` wird probiert.

**`IOServiceOpen` ist selbst kein Nulltarif.** Das Öffnen ruft
`IOService::newUserClient` im Kernel; auf der Beschleunigerfamilie allokiert das
ein Kontextobjekt, also echter Treibercode mit echtem Zustand — vor jedem
Selektor. Das Öffnen zählt als Eingriff, nicht als Vorbereitung.

**SIP zu lockern schreibt NVRAM.** `csrutil` aus recoveryOS setzt
`csr-active-config` persistent und maschinenweit. Das ist eine dauerhafte
Absenkung der Systemsicherheit, die die Messung überdauert — und sie ist **nur**
für Kernel-Probes nötig. Userspace-`pid`-Probes, das Unified Log, `ioreg` und
`system_profiler` brauchen sie nicht. `collect_platform.py` sagt dir, was dein
aktuelles SIP-Wort erlaubt, bevor du irgendetwas änderst.

**`fbt` auf einem geladenen Grafik-Kext ist Kernel-Instrumentierung**, keine
Prozessbeobachtung. Das gehört in eine andere Risikoklasse als ein
`pid$target`-Probe auf `IOConnectCallMethod` und wird hier nie in einem Satz mit
ihm genannt.

**Byte-ausgebende Flags sind im Code verboten**, nicht nur im Ausgabefilter:
`tools/abi_policy.assert_tool_invocation()` weist die Invocation ab, bevor sie
läuft.

| Verboten | Warum |
| --- | --- |
| `dyld_info -section_bytes`, `-all_sections_bytes`, `-disassemble` | gibt Bytes aus |
| `otool -t` ohne `V` | dumpt Sektionsbytes; `-tV` disassembliert und ist erlaubt |
| `llvm-objdump --full-contents`, `-s`, `--show-raw-insn` | gibt Bytes aus |
| `codesign --extract-certificates` | **schreibt** DER-Dateien ins Arbeitsverzeichnis |
| `dyld_shared_cache_util -extract` | schreibt Apple-Binärinhalt auf die Platte |
| `kmutil splitkc` | schreibt Kext-Binaries auf die Platte |
| `dd`, `xxd`, `hexdump` | tun nichts anderes |

Wird eine Kopie doch einmal unvermeidbar (etwa ein Slice aus dem dyld-Cache),
dann **außerhalb** des Git-Baums, und sie wird danach gelöscht — `build/` ist
kein zulässiger Ort, obwohl es ignoriert wird.

## Was damit noch nicht erhoben ist

vtable-**Reihenfolge** und Strukturoffsets über `classSize` hinaus, die
Semantik der Selektoren, das Layout des geteilten Submission-Speichers und das
Metal-Treiber-ABI selbst. Dafür braucht es Laufzeitbeobachtung unter den Regeln des vorigen Abschnitts:
ein `pid`-Probe auf `IOConnectCallMethod` im Metal-Prozess (ohne SIP-Eingriff),
das Unified Log, und Bestätigung ausschließlich der Einträge, die der Probe-Plan
als `handler_runs: false` ausweist. Der entscheidende Punkt ist, dass dabei
nicht mehr gesucht wird: die zu bestätigenden Formen stehen schon in den
rekonstruierten Tabellen.

Und unabhängig davon bleibt wahr: keiner dieser Läufe programmiert Hardware.
`R99` steht in jedem Audit-Bericht.
