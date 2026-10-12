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

Alle Grenzwerte kommen aus dem Vertrag oben; es gibt keine Magic Numbers im
Scanner. Belegt ist das Verfahren durch
[`tests/test_abi_dispatch.py`](../tests/test_abi_dispatch.py): 24 Tests gegen
**selbst gebaute echte Mach-O-Bilder** mit gepflanzter Tabelle — inklusive
gestripptem Binary, Legacy-Struktur und Kernel-Collection. Ein Apple-Binary
beweist das nicht, weil dort die erwartete Antwort unbekannt ist.

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

19 Suiten, 58 gepinnte Quellen, 73 Upstream-Fakten mit Zeilenzitat, plus die
macOS-Stufen (`appledriver`, `system`, `device`), die hier auf dem Mac endlich
laufen.

## Was mir das bringt

| Datensatz | Woraus Treibercode wird |
| --- | --- |
| `platform.json` | ob und wie der Treiber dauerhaft lädt; welche Probes die Maschine zulässt |
| `graphics-inventory.json` | welche Familie unsere `Info.plist` deklarieren muss, welche Basisklasse, welche Methoden zu überschreiben sind, welches Metal-Bundle gegen welchen Client spricht |
| `dispatch-*.json` | die exakte Selektortabelle, die unser User-Client anbieten muss: Nummern, Argumentformen, Entitlements |
| Probe-Plan | wie sich jede dieser Formen am laufenden System bestätigen lässt, bevor wir eine Zeile dagegen schreiben |

## Was damit noch nicht erhoben ist

vtable-**Reihenfolge** und Strukturoffsets über `classSize` hinaus, die
Semantik der Selektoren, das Layout des geteilten Submission-Speichers und das
Metal-Treiber-ABI selbst. Dafür braucht es Laufzeitbeobachtung — dtrace auf
`IOConnectCall*`, lldb an einem Metal-Prozess, bounded User-Client-Probing — und
genau dafür nennt `platform.json` die SIP-Voraussetzungen. Diese Werkzeuge
folgen als nächstes; der entscheidende Punkt ist, dass sie dann nicht mehr
suchen müssen, sondern den Probe-Plan abarbeiten, der aus den rekonstruierten
Tabellen schon folgt.

Und unabhängig davon bleibt wahr: keiner dieser Läufe programmiert Hardware.
`R99` steht in jedem Audit-Bericht.
