# Tracker und Traces auf macOS

Der Diagnosetreiber verfolgt seinen Start, PCI-Erkennung, abgewiesene oder
ausgeführte Registerleseversuche, Client-Verbindungen und CPU-Pufferoperationen.
Jeder Trace besitzt Sequenz, rohe Uptime-Ticks, feste Ereignisnummer, IOReturn-Status,
numerischen Verbindungskontext und zwei numerische Argumente. Keine Zeiger,
Speicheradressen, Namen oder Seriennummern werden als Trace-Felder exportiert.

Der Ring enthält höchstens 128 Ereignisse; ein ABI-1-Aufruf liefert maximal 16.
Überschriebene Einträge werden gezählt. `ticks` sind keine Millisekunden und keine
Kalenderzeit. Kernelmeldungen haben denselben festen Zahlenaufbau und sind auf
256 Zeilen pro Treiberinstanz begrenzt. Im Standardmodus werden Start, Bereitschaft,
Stop und Fehler gemeldet; `LBDebugLogging=true` ergänzt erfolgreiche Operationen.

Der Ring liegt im flüchtigen Treiberspeicher. Nach einem Neustart oder Kernelpanic
ist er nicht erhalten; es gibt noch keine bestätigte crashfeste Trace-Speicherung.
Welche eigenen Kernelmeldungen einen Absturz überleben, muss nativ geprüft werden.

## Sammeln

Nach tatsächlichem Laden der Diagnose-Kext unter Tahoe, mit Python 3:

```sh
sudo python3 tools/collect_macos.py --out ./diagnostics
sudo python3 tools/collect_macos.py --out ./diagnostics-memory --memory-test
sudo python3 tools/collect_macos.py --out ./diagnostics-dma --dma-test
```

Der Sammler öffnet ausschließlich `LunarBridgePCI`. Administratorrecht wird am
nativen User-Client geprüft. Ohne `--memory-test` oder `--dma-test` fordert er keine Puffer an.
Der optionale Test prüft einen eigenen 4-KiB-CPU-Puffer auf Nullinitialisierung,
schreibt/liest zwei Randbytes und löst die Abbildung wieder. Es gibt weder
GPU-Bindung noch GPU-Befehle. Ein Fehler dieser Probe lässt Snapshot und Traces
im Bericht erhalten; auch ein gescheitertes Trace-Lesen verwirft keinen Snapshot.

`--dma-test` erfordert zusätzlich `LBEnableDMAPreparation=true` im geladenen
Bundle. Die Probe verlangt die 32-Byte-DMA-Ausgabe von Selector 5 und prüft
Vorbereitung und Seitenzahl. Sie bindet keine GPU-Tabellen. Ein gewöhnlicher
CPU-Test wird bei aktivierter DMA-Eigenschaft vor der Allokation gesperrt.
Freigabe und IOMMU-Verhalten müssen gesondert qualifiziert werden;
`dma_cleanup_verified` bleibt false. Siehe [DMA-Anschluss](DMA_BACKEND.md).

Der Sammler schreibt `diagnostics.json` und `FEHLERBERICHT.md`. Er erfasst
macOS-Version/Build, die erlaubten LunarBridge-Registryfelder, den ABI-Snapshot,
Traces und nur die streng dekodierten eigenen Kernelmeldungen. Allgemeine
Systemlogs, SMBIOS, Plattformseriennummern, Konten und rohe Registrybäume werden
nicht gespeichert. `--minutes 1` bis `--minutes 60` begrenzt das Log-Zeitfenster.
Fehlt der Snapshot, wird der verfügbare Bericht trotzdem gespeichert und der
Sammler beendet sich mit Status 2. Unter Windows wird die Ausführung abgewiesen.

Bei `LBEnableForceWakeProbe=true` erfasst der Sammler außerdem den 80-Byte-Status
von Selector 6. Handshake und Cleanup werden getrennt berichtet. Bei ungeklärtem
Abbau werden gehaltene Ressourcen als Fehler markiert; ein ACK bestätigt keine
Shader-Ausführung oder Metal. Siehe [GPU-Anschluss](GPU_BRINGUP.md).

Der Sammler liest zusätzlich die 576-Byte-Arc-Operationstabelle von Selector 7
nach `arc_interface`. Der Dekoder weist eine Tabelle ab, die Ausführung oder
Metal behauptet, unbekannte Bits führt oder Deskriptoren mit Skalaren meldet.
Scheitert der Aufruf, bleibt der Bericht erhalten und trägt
`arc_interface_error`. Die Tabelle beschreibt nur Schnittstellenformen; sie
belegt keinen geladenen GPU-Pfad. Siehe
[Schnittstellenvergleich](ARC_INTERFACE_DIFF.md).

Auswertung eines vorhandenen Berichts funktioniert auch unter Windows:

```sh
python3 tools/analyze_trace.py diagnostics/diagnostics.json --out FEHLERBERICHT.md
```

## Ereignisse und Fehlerstelle

| ID | Ereignis | Bedeutung / nächste Prüfung |
| --- | --- | --- |
| 1 / 2 / 7 / 8 | Start / PCI erkannt / bereit / Stop | Vollständigkeit des Lebenszyklus |
| 3 | Registerlesen gesperrt | Policy im Snapshot; fehlendes Opt-in ist im Standardmodus erwartet |
| 4 | BAR-Mapping fehlgeschlagen | BAR-Länge, D0 und Speicherfreigabe |
| 5 / 6 | GMD gelesen / Werte verworfen | Rohwerte und Decodierung; Lesen allein beweist keine Beschleunigung |
| 20 / 21 / 29 | Client offen / geschlossen / abgewiesen | Kontext, Administratorrecht, Verbindungslimit |
| 22 / 23 / 24 | Puffer angefordert / angelegt / gescheitert | Größe, aktives Budget und IOKit-Status |
| 25 | CPU-Mapping angefordert | Kein Beweis einer erfolgreichen User-Abbildung; Sammlertest ergänzen |
| 26 | Methode abgewiesen | Selector, Strukturgröße, ABI und Status |
| 27 | GPU-Submit nicht implementiert | `Unsupported` ist in dieser Version erwartet |
| 28 | Treiberreferenz auf Puffer freigegeben | Belegt keine vollständige Freigabe noch gehaltener IOKit-Mappings |
| 40 / 41 | DMA vorbereitet / gescheitert | Geräte-Mapper, Descriptorstatus, Seitenzahl und Grenzen |
| 42 / 43 | DMA-Referenzen freigegeben / Freigabefehler | Keine GPU-Ausführung; Fehlerressourcen nicht als frei zählen |
| 50 / 51 | Main-GT-Handschlag / Fehler | GMD-IP, Primärstatus und Put getrennt auswerten |
| 52 / 53 | Wake-Cleanup / Cleanupfehler | BAR-/Provider-/Servicereferenzen bleiben bei ungeklärtem Put gehalten |
| 60 | Arc-Schnittstellentabelle gelesen | `arg0` ist die Operationszahl; kein GPU-Pfad belegt |
| 61 / 62 | Arc-Anfrage akzeptiert / abgelehnt | `arg0` ist die Operation, `arg1` enthält Ergebnis und Grund; Annahme ist keine Submission |

Der Fehlerbericht nennt die zugehörigen Quelldateien/Funktionen und unterscheidet
erwartete Einschränkungen von Fehlern. Ein gesetztes Capability-Bit wird nicht
als bestandener Metal-Test gewertet. Synthetische Testberichte tragen ausdrücklich
`synthetic=true`. Die 27 Windows-Prüfungen der Werkzeuge benutzen simulierte IOKit-
Aufrufe; sie bestätigen keine tatsächliche macOS-Verbindung oder Gerätefunktion.

Die [Issue-Vorlage](../.github/ISSUE_TEMPLATE/native-diagnostics.yml) ermöglicht
das Zuordnen eines Gerätefehlers zu Commit, Phase und bereinigten Ereignissen.
Der [Prüfbericht](VALIDATION.md) hält die bestandenen Prüfungen und deren Grenzen fest.
