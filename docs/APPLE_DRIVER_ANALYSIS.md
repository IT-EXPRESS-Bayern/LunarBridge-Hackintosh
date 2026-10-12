<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Den letzten kompatiblen Apple-Treiber analysieren

Stand: 12. Oktober 2026. Dieses Dokument beschreibt, was eine Analyse des
jüngsten Apple-Intel-Grafiktreibers (Ice Lake / Gen11) für dieses Projekt
leisten kann, was nicht, und mit welchem Werkzeug sie läuft:
[`tools/analyze_apple_driver.py`](../tools/analyze_apple_driver.py).

**In diesem Repository liegt kein Apple-Binary und es wird keines
heruntergeladen oder weitergegeben.** Die Analyse läuft auf der Maschine, auf
der der Treiber bereits installiert ist; eingecheckt wird nur die
Schnittstellenkarte.

## Was die Analyse bringt — und was nicht

Der falsche Zielgedanke wäre, die Hardwareprogrammierung des Gen11-Treibers zu
gewinnen: die ist für Xe2 wertlos. Ice Lake hat andere Seitentabellen, andere
Submission, eine andere ISA. Diese Seite liefert weiterhin der
[Linux-Port](ARC_PORT_FROM_LINUX.md).

Wertvoll ist die **macOS-Seite**, also genau das, was dem Projekt fehlt:

| Frage | Antwort steckt in |
| --- | --- |
| Welche privaten Familien linkt ein Metal-fähiger Grafiktreiber? | `OSBundleLibraries` des Bundles |
| Welche Klassen registriert er, und welche fremden Metaklassen referenziert er? | Metaklassen-Symbole, definiert und undefiniert |
| Welche IOKit-Methoden muss er überschreiben? | Definierte Symbole, gefiltert auf `start`, `newUserClient`, `externalMethod`, `clientMemoryForType`, `setPowerState` … |
| Wie ist die Personality geformt (Provider, Match-Kategorie, Userclient-Klasse)? | `IOKitPersonalities` |
| Welche Plug-ins gehören dazu (Metal-Treiberbundles, Shader-Compiler)? | `Contents/PlugIns` |

Das sind die Angaben, mit denen man die eigene `Info.plist`, die
Klassenhierarchie und die Liste der zu implementierenden Methoden belegen kann,
statt sie zu raten. Der heutige Stand dieses Projekts ist dagegen die
Accelerator-Hälfte ohne Displayhälfte und ohne Metal-Anbindung; welche
Basisklasse und welche Familienabhängigkeit dafür nötig sind, steht in keiner
veröffentlichten Quelle — der [Schnittstellenvergleich](ARC_INTERFACE_DIFF.md)
hält genau dieses Loch fest.

Ein konkreter Hinweis aus der Gegenprüfung des veröffentlichten Headers:
`IOFramebuffer.h` enthält mit `setBackingFramebuffer(const IOPixelInformation *,
uint32_t bufferCount, void *mappedAddress[])` und
`switchBackingFramebuffer(uint32_t)` zwei **konkrete, nicht virtuelle** Methoden
— der veröffentlichte Weg, IOGraphics einen Backing-Puffer zu übergeben. Für
einen Treiber, der zunächst nur ein Display anmelden statt beschleunigen will,
ist das die erste Spur, die es zu prüfen gilt. Ob sie ohne die privaten
Includes tragfähig ist, ist damit nicht beantwortet.

**Was Symbolnamen nicht hergeben:** vtable-Reihenfolge, Strukturoffsets,
Selektortabellen und Methodensemantik. Dafür bräuchte es Disassemblierung oder
Laufzeitbeobachtung; das Werkzeug tut beides nicht. Eine Karte der Namen ist
der Anfang einer Portierung, nicht ihr Ergebnis.

## Rechtlicher Rahmen

Analyse einer Schnittstelle, die ein eigener Treiber erfüllen muss, für
Hardware, die der Hersteller nicht unterstützt, ist der Fall, den
**§ 69e UrhG** beziehungsweise **Art. 6 der EU-Softwarerichtlinie** beschreibt:
Dekompilierung zur Herstellung von Interoperabilität, durch einen rechtmäßigen
Nutzer, soweit die Information nicht anders zugänglich ist, beschränkt auf die
dafür notwendigen Teile und nicht zur Entwicklung eines konkurrierenden
Programms. Die US-Rechtslage kennt mit Fair Use und Sega v. Accolade
beziehungsweise Sony v. Connectix vergleichbare Ergebnisse für
Interoperabilitäts-RE.

Daraus folgen die Regeln, die das Werkzeug erzwingt:

1. Es liest nur eine **lokal installierte** Kopie, die der Ausführende
   rechtmäßig besitzt. Es lädt nichts herunter.
2. Es gibt **keine Bytes** des Bundles aus: Namen, Zahlen, Offsets, Hashes.
3. Das Ergebnis dient dazu, eigene Schnittstellen zu schreiben — nicht dazu,
   Apple-Code zu kopieren oder einen Apple-Treiber zu ersetzen, für den Apple
   diese Hardware ohnehin nicht unterstützt.
4. Kein Apple-Binary, kein Shader-Compiler-Bundle und keine Firmware gelangt
   ins Repository oder in ein Release.

Dies ist keine Rechtsberatung. Wer die Karte veröffentlicht, trägt die
Entscheidung; die Punkte 1 bis 4 sind die Linie, die dieses Projekt zieht.

## Ablauf

Auf dem Mac (oder dem Hackintosh) mit installiertem Treiber:

```sh
python3 tools/analyze_apple_driver.py \
    --kext /System/Library/Extensions/AppleIntelICLGraphics.kext \
    --out build/apple/icl-graphics-map.json \
    --markdown build/apple/icl-graphics-map.md
```

Sinnvoll sind drei Läufe: der Accelerator (`AppleIntelICLGraphics.kext`), der
Framebuffer (`AppleIntelICLLPGraphicsFramebuffer.kext`) und zum Vergleich die
Familie, gegen die beide linken. Ohne `nm` im Pfad — also auf jeder Maschine,
die den Treiber nicht hat — liefert das Werkzeug nur Bundle-Metadaten und sagt
das ausdrücklich, statt Symbole zu erfinden.

Eingecheckt wird die erzeugte Karte, nicht das Bundle. `tests/test_apple_driver_analysis.py`
prüft mit synthetischen Bundles, dass die Karte keine Bundle-Inhalte trägt, dass
verschachtelte Plist-Werte auf ihren Typnamen reduziert werden und dass ein
fehlendes Binary gemeldet statt geraten wird.

## Was danach damit passiert

1. Aus `OSBundleLibraries` und den referenzierten Metaklassen ergibt sich die
   Zielhierarchie: welche Basisklasse ein Accelerator haben muss und welche
   Familie die eigene `Info.plist` deklarieren müsste.
2. Die Override-Liste wird zum Implementierungsplan für den eigenen Treiber —
   jede Methode einzeln, mit eigener Implementierung gegen die portierte
   Xe2-Hardwareseite.
3. Der Vergleich gegen den heutigen Stand zeigt, welche Lücke zwischen
   Diagnosetreiber und Metal-fähigem Treiber wirklich liegt. Erst dann ist
   belegbar, wie groß der Metal-Schritt ist, statt ihn zu schätzen.
