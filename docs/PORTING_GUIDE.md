# An anderen Grafikeinheiten arbeiten

Dieser Leitfaden ist für Entwickler, die einen späteren qualifizierten
LunarBridge-Grafikpfad auf weitere Hardware übertragen möchten. Stand
11. Oktober 2026 fehlt bereits für den ersten Arc-130V-Zielrechner die native
GPU-Ausführung und Metal-Registrierung. Hier wird daher eine überprüfbare
Arbeitsmethode beschrieben, keine fertige Unterstützung anderer GPUs.

## 1. Hardware und Generation eindeutig bestimmen

Erfasse PCI-Vendor/Device, Subsystem und Revision, tatsächliche Grafik-/Media-IP,
BAR-Aufteilung, Speicherarchitektur, Firmware- und Displaytopologie. Eine ähnliche
Marketingbezeichnung oder derselbe Hersteller reichen nicht. Ein neuer Device-
Match darf zunächst ausschließlich einen eingegrenzten Diagnosepfad aktivieren.
Ein fremdes Gerät erhält keine Lunar-Lake-Registerzugriffe durch ein ID-Spoofing.

Dokumentiere die Quellen als exakte Commits mit Dateihashes und Lizenz. Falls die
Generation oder ein Registervertrag unklar ist, endet die betreffende Phase mit
einem expliziten ungeklärten Zustand. Ein erfolgreiches Tool- oder Formatresultat
ersetzt die Hardwareprobe nicht.

## 2. Wiederverwendbaren Code von Hardwareverträgen trennen

| Bereich | Häufig wiederverwendbar | Neu zu qualifizieren |
| --- | --- | --- |
| Diagnose | Begrenzte Wire-ABI, Trace-Cursor, private Datenfilter, Fehlerberichte | Geräteselektion, sichere Registersätze und Powerzustände |
| IOKit-Ressourcen | Geräte-DMA-Mapper, Besitzer-/Tokenverwaltung, geordnete Freigabe | BARs, IOMMU-Adressgrenzen, Hardware-Lifetime, Power-/Resetpfad |
| Wake/Initialisierung | Zeitbegrenzter Request/ACK-Mechanismus | Domains, Register, Masked-Writes, Workarounds und Powersequenz |
| Speicher | Reservierungs-/Überlauf-/Segmentprüfungen | Pagegrößen, PTE/PAT, Speicher-Domain, GGTT/PPGTT, WOPCM und TLB |
| Firmware | Begrenzte Container- und Versionsprüfung | Richtige Firmware, Authentifizierung, Bootfolge, ADS und ABI |
| Shader | Eigener Referenzkernel, unabhängige Ergebniskontrolle | ISA, Compilerbackend, Zebin/Reflection/Payload und Entry-Auswahl |
| Scheduling | Besitzerzustand, Größenprüfung, atomare CPU-Ausgabe | Ring/LRC, Engineklassen, GuC-Queues, Fence/IRQ/Reset und Synchronisation |
| Metal/Display | Testprogramme und Nachweisformat | Metalanbieter, Grafik-ABI, Displayengine, Panel-/Link-/HDR-Verträge |
| Installer | Sicherung, Konfliktprüfung und Rücknahme | Bundle-ID, Personality, Kernelbereich, Abhängigkeiten und Bootqualifikation |

Andere GPUfamilien können andere Firmware-, Speicher- und Befehlsmodelle haben.
Ein LNL-PAT-Index oder Xe2-WALKER ist für sie kein Standard. Für einen Port sind
neue Plattformmodule und enge Personalities vorgesehen. Andere Geräteklassen
bleiben separate Treiber; ein Grafikport wird kein All-in-one-Hardwaretreiber.

## 3. Zuerst Format und CPU-Verträge, dann Gerätezugriffe

Beginne mit kleinen, freistehenden Kernfunktionen. Prüfe reale Encoder/Decoder mit
festen Primärvektoren, Grenzfällen, veralteten Tokens, Überläufen und fehlerhaften
Eingaben. Fehler müssen Ausgabepuffer und Ressourcen in dem dokumentierten Zustand
lassen. IOKit-Doubles sind zulässig, müssen als Simulation ausgewiesen werden.

Beispiel Xe2: GGTT-Cachebits und das Gerätespeicherbit unterscheiden sich von
PPGTT. Der aktuelle [GGTT-Plan](GGTT_BINDING.md) prüft segmentierte DMA-Seiten
erneut und erzeugt eigene GGTT-Einträge. Er behauptet dabei keine globale
Hardwarebelegung, BAR-Schreibausführung oder TLB-Invalidierung.

Ein Speicher- oder Submissionpfad akzeptiert keine ungeprüfte physische Adresse
als Ersatz für den Geräte-DMA-Mapper. GPU-Adressen werden erst nach tatsächlicher
nativer Bindung als aufgelöst behandelt. Firmware-/Queue-Erfolg darf nicht aus
einem fertig kodierten CPU-Befehl abgeleitet werden.

## 4. Die Compilerkette der Zielhardware nachweisen

Bewahre die Stufen getrennt auf: Eingangsquelle, AIR oder SPIR-V, Reflection,
native ISA, Payload, Relokationen und benötigte Heaps. Prüfe tatsächlich
erzeugte Dateien mit den vorgesehenen Formaten und einem unabhängigen Validator.
Werte Schema-Versionen aus der implementierten, gepinnten Schnittstelle aus.

Unser eigener Windows-Kernel hat eine Intel-OpenCL-Zebin-ABI. Vulkan-Shader-
SPIR-V aus der [Metal-Compilerbrücke](METAL_SHADER_BRIDGE.md) kann nicht direkt
mit diesem Payload gestartet werden. Ein Port benötigt den passenden nativen
Backend und ein neues belegtes Speicher-/Dispatchlayout.

Verwende zuerst einen eigenen begrenzten Integer-Kernel mit unabhängiger
Ergebniskontrolle und ohne CPU-Ersatz. Archivierte Resultate müssen GPUidentität,
Quell-/Compilerstand, Completion und Fehler enthalten. Ein Compilerlauf ist
kein Dispatch; ein Dispatch ist kein verfügbarer Metalanbieter.

## 5. Native Meilensteine einzeln qualifizieren

1. **Build:** Echter Kext-Link mit passender Architektur, Kennung und exaktem
   Quellstand. Ein Mach-O-Objekt allein ist kein geladenes Treiberbundle.
2. **Gerätezulassung:** Kext auf dem Zielgerät geladen, richtige Personality,
   nachvollziehbarer Provider und reproduzierbare Traces.
3. **Speicher:** Gültige DMA-Lifetime, bestätigte Cache-/PAT-Einstellung,
   Adressraumbelegung, Seitentabellen und TLB/Barrieren.
4. **Firmware und Queue:** Laufende authentifizierte Firmware, passende ABI,
   Engine-/ADS-Zuordnung, korrekte LRC-/Ring-/Fence-/Interruptverträge.
5. **Eigener GPU-Kernel:** Verifizierte Ergebnisse, Completion sowie geordnete
   Freigabe nach Erfolg und nach Timeout/Reset.
6. **Metal:** Tatsächlicher Geräteanbieter, korrekte Metal-Ressourcen und
   Synchronisation; ein Client, der `MTLDevice` abfragt, registriert keinen Treiber.
7. **Display:** Framebuffer, Panel-/Linktraining, Modes, Sleep/Wake und danach
   HDR/Video unter den belegten Hardwarebedingungen.

Das vorhandene sichere Rücknahmeverfahren gehört zu jeder Geräteprobe.
Die erste Installation ist als eigenständiger Testaufbau zu dokumentieren.
Sicherheitsoptionen und Firmware-Secure-Boot werden getrennt ausgewiesen; eine
Compiler- oder PCI-Probe liefert dafür keinen Nachweis.

## 6. Einen Port für die Community veröffentlichen

Eine nachvollziehbare Änderung enthält Geräte/IP-Profil, Problem und konkrete
Vorher-/Nachher-Funktion, verwendete Primärdefinitionen, eigene Abweichungen,
Lizenzattribution, ABI-/Lebensdauerkontrakte und Testergebnisse je Plattform.
Notiere fehlende, fehlgeschlagene und bestandene Stufen ausdrücklich. Ein neuer
Port erhält erst nach Gerätequalifikation eine Supportkennzeichnung.

Reiche keine privaten Inventare, Seriennummern, Schlüssel oder ungefilterten
Speicheradressen ein. Öffentliche Beispiele sind nachvollziehbar anonymisiert
und dürfen nicht als echte Hardwareproben etikettiert werden.

Eigene LunarBridge-Dateien bleiben GPL-3.0-only. Fremder Quellcode, Header,
Compiler und Firmware behalten ihre eigenen Lizenzen und Rechte. Vor dem
Übernehmen fremder Dateien sind diese zu prüfen und ihre Herkunft zu erhalten.
Reproduzierbare Build- und Testanweisungen sowie Änderungen am Treiberaufbau
werden in [ARCHITECTURE.md](ARCHITECTURE.md) und den Modulunterlagen gepflegt.
