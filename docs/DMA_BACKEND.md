# IOKit-DMA-Anschluss

`native/LBDMAContext.cpp` bereitet den privaten CPU-Puffer einer Verbindung über
`IOMapper::copyMapperForDevice` und `IODMACommand` vor. Der Mapper muss zum
exakten Samsung-PCI-Anbieter gehören. Ohne diesen Mapper wird die Anfrage
abgewiesen; physische Segmente und der globale Systemmapper sind kein Ersatz.
Die Personality hat `LBEnableDMAPreparation=false`. Der Standardbuild verwendet
weiter ausschließlich den bisherigen CPU-Puffer.

## Ablauf und Grenzen

1. Eigentümer, Deskriptorlänge, 4-KiB-Alignment und höchstens 1 MiB prüfen.
2. Geräte-Mapper und Deskriptorreferenz halten; Speicher für In/Out vorbereiten.
3. `IODMACommand` mit 46 Adressbits, 4-KiB-Segmenten und dem Geräte-Mapper anlegen.
4. Deskriptor zuordnen und den angeforderten Bereich explizit vorbereiten.
5. Höchstens 256 vollständige Segmente erfassen; Fortschritt, Länge, Alignment,
   Überlappung und Adressgrenzen prüfen.
6. Die vorhandene eigentümergebundene VA-Arena verwenden und Xe2-PTE-Werte
   ausschließlich in einem CPU-Puffer erzeugen. Erst vollständiger Erfolg
   setzt den vorbereiteten Zustand.
7. DMA vollständig abschließen, Deskriptor trennen, Speicher abschließen und
   gehaltene Referenzen freigeben. Fehlgeschlagene Freigaben werden erfasst;
   betroffene Ressourcen dürfen nicht erneut als verfügbar gelten.

Vor jeder Clientaufnahme wird einer von acht Ownership-Slots reserviert. Bei
dauerhaft fehlgeschlagenem Cleanup übernimmt dieser Slot den heap-eigenen
Kontext, den CPU-Puffer, Byte- und Clientbuchung sowie die bereits gehaltene
Servicereferenz gemeinsam. Der User-Client gibt diese Daten danach nicht frei.
Eine neue administrative Verbindung versucht vor ihrer eigenen Aufnahme die
Quarantäne erneut zu bereinigen. Erst erfolgreicher Abschluss und ein vollständig
leerer Kontext geben Slot, Budget, Puffer und Servicereferenz genau einmal frei.
Der Kontextdestruktor führt keinen versteckten weiteren Cleanup-Versuch aus.

Quarantäne und laufender Retry zählen gegen dieselben acht Aufnahmen und 8 MiB
Buchungsbudget. Ein Stop sperrt neue Aufnahmen und bezeichnet gehaltene
Ressourcen nicht als freigegeben. Nach Provider-Stop bleibt die Quarantäne
gehalten; sie verhindert keine Hardwareabschaltung und belegt keinen sicheren
IOMMU-Abbau. Ein unerwarteter interner Ownership-Widerspruch zerstört keine
noch gehaltenen Mappings. Die dafür nötige native Parallelitäts-/Stop-/Unload-
Qualifikation ist weiterhin offen. Registry-Zähler sind Diagnosewerte und
werden nicht für sicherheitsrelevante Ownership-Entscheidungen verwendet.

Der Adapter veröffentlicht keine Tabellenwurzel, programmiert weder PAT noch
GGTT, führt keine TLB-Invalidierung aus und reicht keine GPU-Befehle ein.
PAT-Index null ist ein Eingabewert für die CPU-Formatprüfung, kein Nachweis einer
passenden Cache-Programmierung. Der User-Client exportiert keine DMA-Adressen,
Segmente oder PTEs. Eine erfolgreiche DMA-Vorbereitung ist keine GPU-Bindung.

Der Client-IOLock serialisiert den Adapter auf dem aufrufenden Clientthread.
Mapper-/DMA-Vorbereitung findet außerhalb des Service-IOLock und außerhalb
eines Workloop-Command-Gates statt. Der Stopzustand wird anschließend erneut
geprüft. Native Power-Transitions, IOMMU-Verhalten und gleichzeitiger Clienttod
benötigen dennoch echte Geräteprüfungen.

## Explizite native Probe

Ein separat angeforderter Versuchsbuild kann die Plist-Eigenschaft setzen:

```sh
python3 tools/build_native.py --sdk /path/to/MacKernelSDK --out build/dma-probe --dma-prepare
```

Das setzt die Eigenschaft im erzeugten Bundle. Es installiert oder lädt das
Bundle nicht. Nach qualifiziertem nativem Laden lautet die Probe:

```sh
sudo python3 tools/collect_macos.py --out ./diagnostics-dma --dma-test
```

Der Sammler fordert ausdrücklich einen eigenen 4-KiB-Puffer an und prüft
Selector 5 (`DMAInfo`, 32 Byte) vor dem CPU-Randbyte-Test. Er verlangt einen
vorbereiteten Geräte-Mapper, genau eine Seite und eine PTE. Snapshot-Flag 32
kennzeichnet ausschließlich das aktivierte Opt-in. Ein gewöhnlicher
`--memory-test` wird bei aktiviertem DMA-Opt-in vor der Pufferanforderung
gesperrt; er kann keine DMA-Probe versehentlich auslösen.

`dma_cleanup_verified=false` bleibt im Ergebnis, weil die erfolgreiche
CPU-Abbildung/Freigabe und das Schließen des User-Clients keinen vollständigen
DMA-Abschluss beweisen. Ereignisse 40–43 verfolgen Vorbereitung, Fehler und
Freigabe. Ein Fehlerbericht muss auch spätere Cleanup-Ereignisse berücksichtigen.

## Vorliegende Belege

30 Tests führen denselben C++-Adapter unter Windows mit ausdrücklich simulierten
IOKit-Objekten aus. Sie prüfen Größen/Eigentümer, fehlenden Mapper, jeden
Vorbereitungsfehler, beschädigte Segmente, atomaren Erfolgszustand, Freigabereihenfolge
und vorübergehend sowie dauerhaft gescheiterten Cleanup. Zehn weitere Tests
führen die tatsächliche Ownership-Tabelle aus und prüfen die acht Slots,
atomaren Transfer, exklusiven Retry und die Freigabe erst nach tatsächlichem
Adapter-Cleanup. IOKit-Aufrufe sind simuliert; native Kernel-Nebenläufigkeit ist
damit nicht getestet. Weitere Python-Prüfungen
testen ABI, Datenschutz und die beiden erforderlichen Opt-ins.
Die eigentlichen nativen Quellen kompilieren gegen die gepinnten SDK-Header.
Diese Belege bestätigen keine IOMMU-Abbildung am Samsung, keine native
Kext-Annahme und keine Grafikbeschleunigung.

Apples veröffentlichte Tahoe-26.0-Exportverträge enthalten den Geräte-Mapper
und die benötigten Intel-DMA-Anschlüsse. Das inline implementierte
`gen64IOVMSegments` verwendet das tatsächlich importierte `genIOVMSegments`.
Eine Quell-Exportliste bestätigt noch nicht die ABI des ausgelieferten Kernels.
Der getrennte [Schnittstellenprüfer](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Native-Compatibility)
unterscheidet diesen Quellvertrag von tatsächlich ausgewählten Provider-Binärdateien.

Primärquellen: [Apple IOMapper](https://developer.apple.com/documentation/kernel/iomapper),
[Apple IODMACommand](https://developer.apple.com/documentation/kernel/iodmacommand),
[Tahoe IOKit-Exporte](https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/config/IOKit.exports),
[Tahoe Intel-Exporte](https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/config/IOKit.x86_64.exports).
