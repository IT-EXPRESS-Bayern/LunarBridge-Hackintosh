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
```

Der Sammler öffnet ausschließlich `LunarBridgePCI`. Administratorrecht wird am
nativen User-Client geprüft. Ohne `--memory-test` fordert er keine Puffer an.
Der optionale Test prüft einen eigenen 4-KiB-CPU-Puffer auf Nullinitialisierung,
schreibt/liest zwei Randbytes und löst die Abbildung wieder. Es gibt weder
GPU-Bindung noch GPU-Befehle. Ein Fehler dieser Probe lässt Snapshot und Traces
im Bericht erhalten; auch ein gescheitertes Trace-Lesen verwirft keinen Snapshot.

Der Sammler schreibt `diagnostics.json` und `FEHLERBERICHT.md`. Er erfasst
macOS-Version/Build, die erlaubten LunarBridge-Registryfelder, den ABI-Snapshot,
Traces und nur die streng dekodierten eigenen Kernelmeldungen. Allgemeine
Systemlogs, SMBIOS, Plattformseriennummern, Konten und rohe Registrybäume werden
nicht gespeichert. `--minutes 1` bis `--minutes 60` begrenzt das Log-Zeitfenster.
Fehlt der Snapshot, wird der verfügbare Bericht trotzdem gespeichert und der
Sammler beendet sich mit Status 2. Unter Windows wird die Ausführung abgewiesen.

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

Der Fehlerbericht nennt die zugehörigen Quelldateien/Funktionen und unterscheidet
erwartete Einschränkungen von Fehlern. Ein gesetztes Capability-Bit wird nicht
als bestandener Metal-Test gewertet. Synthetische Testberichte tragen ausdrücklich
`synthetic=true`. Die 13 Windows-Prüfungen der Werkzeuge benutzen simulierte IOKit-
Aufrufe; sie bestätigen keine tatsächliche macOS-Verbindung oder Gerätefunktion.

Die [Issue-Vorlage](../.github/ISSUE_TEMPLATE/native-diagnostics.yml) ermöglicht
das Zuordnen eines Gerätefehlers zu Commit, Phase und bereinigten Ereignissen.
Die [Aufgabenliste](../TASKS.md) enthält offene Implementierungs- und Geräteproben.
