<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Recon: ein Lauf, der alles prüft und die wichtigen Punkte extrahiert

Stand: 12. Oktober 2026. [`tools/recon.py`](../tools/recon.py) ist der eine
Einstiegspunkt, der in einem Lauf baut, testet, die Oberflächen vergleicht, die
Hardwarefakten aus den gepinnten Quellen neu herleitet, den installierten
Apple-Treiber kartiert, den lebenden Grafikstapel liest — und am Ende eine
nach Dringlichkeit sortierte Befundliste schreibt.

```sh
python3 tools/recon.py --out build/recon                      # alles, was hier geht
python3 tools/recon.py --out build/recon --stages extract      # nur die Extraktion
python3 tools/recon.py --out build/recon --no-tests            # ohne Testläufe
python3 tools/recon.py --out build/recon \
    --kext-root /System/Library/Extensions                     # auf dem Mac
```

Ergebnis sind `build/recon/recon.json` (vollständig, maschinenlesbar),
`build/recon/RECON.md` (lesbarer Bericht), `upstream-facts.json` und
`interface-extraction.json`. Der Exit-Code ist 1, wenn der Lauf einen **Defekt**
findet: Quellendrift, fehlgeschlagener Build, fehlgeschlagene Suite,
abweichende Tabelle. Offene Portierungsarbeit ist kein Defekt und lässt den
Lauf grün.

## Die acht Stufen

| Stufe | Was sie feststellt | Läuft |
| --- | --- | --- |
| `environment` | Toolchain, OS, SIP-Status, `boot-args` | überall; die macOS-Abfragen nur dort |
| `repository` | Commit, Arbeitsbaum, SHA-256 aller 58 gepinnten Quellen, dazu beide Bibliotheken und 15 Testsuiten | überall |
| `interface` | Xe-uAPI gegen den macOS-Kontrakt, kompilierte Tabelle gegen die Quellen | überall |
| `portplan` | was die portierten PAT- und LRC-Module programmieren **würden** | überall |
| `extract` | die Hardwarefakten neu hergeleitet, jede mit Zeilenzitat | überall |
| `appledriver` | Bundles, Familien, Klassen und Overrides des installierten Apple-Treibers | nur auf dem macOS-System mit dem Treiber |
| `system` | geladene Grafik-Kexte, `IOAccelerator`- und `IOGPU`-Knoten, Displays | nur macOS |
| `device` | der Arc 130V im PCI-Baum und die Diagnoseselektoren des eigenen Treibers | nur macOS |

Keine Stufe rät. Wo sie nicht laufen kann, schreibt sie `performed: false` und
den Grund — und genau dieser Grund steht im Bericht, statt einer Lücke.

## Warum die Extraktion deklarativ ist

Die Stufe `extract` ist eine Tabelle von Regeln `(Gruppe, Name, gepinnte Datei,
Muster, Typ)`. Jede Regel liefert einen Wert **und** das Zitat
`Datei:Zeile`, aus dem er stammt. Trifft ein Muster nicht mehr, ist das kein
stiller Fehler, sondern ein Befund: `R13` wird `high` und der Lauf schlägt fehl.
So wird aus „die Quelle hat sich geändert" ein CI-Fehler statt einer falschen
Konstante im Treiber.

PAT und LRC werden nicht doppelt geparst, sondern von denselben Klassen
(`PinnedPatSource`, `PinnedLrcSource` aus
[`tests/test_arc_port.py`](../tests/test_arc_port.py)), gegen die der Port
getestet wird. Bericht und Test können über diese Werte also nicht
auseinanderlaufen.

Heutiger Stand: **31 Regeln, 73 Fakten, keine Drift** — PCI-IDs und
`GRAPHICS_VERx100`, GGTT-/PPGTT-Seitenbits, Forcewake-Quittierung,
GuC-Register samt Lunar-Lake-Firmwareversion, die vollständige Xe2-PAT-Tabelle
und das LRC-Layout.

## Was der Lauf beweist — und was nicht

Er beweist: die Module bauen mit `-Werror`, alle 22 Suiten (547 Tests) laufen
durch, die kompilierte Tabelle deckt sich mit den gepinnten Quellen, und jeder
Zahlenwert im Port lässt sich auf eine Zeile Upstream zurückführen.

Er beweist **nicht**: irgendetwas über Hardware. `R99` steht in jedem Bericht
und sagt genau das: kein Register geschrieben, keine Queue registriert, kein
Metal-Gerät erzeugt. Die Reihenfolge der nächsten Schritte steht als `next_step`
an `R14` — PPGTT-Wurzel über dem GGTT-Planer, dann die GuC-Ladesequenz
(RSA-Upload, `DMA_CTRL`, `GUC_STATUS`-Poll), jeweils mit eigenem
Readback-Test.

## Die Befund-Kennungen

| ID | Thema |
| --- | --- |
| `R01` | gepinnte Quellen verifiziert |
| `R02` | Bibliotheken gebaut |
| `R03` | Testsuiten |
| `R04` | kompilierte Schnittstellentabelle gegen die Quellen |
| `R05` | Apple hat keinen Xe-/Xe2-Treiber |
| `R06` | Inhalt des Portplans |
| `R07` | Apple-Bundles gefunden und kartiert |
| `R08` | überschriebene IOKit-Methoden |
| `R09` | Symbolwerkzeug fehlte |
| `R10` | eigener Treiber geladen |
| `R11` | Zielgerät im Gerätebaum |
| `R12` | Diagnoseselektoren antworten |
| `R13` | Extraktionsdrift |
| `R14` | extrahiert, aber im Repository noch offen |
| `R99` | keine Hardwarebeschleunigung |

## Grenzen, die der Lauf einhält

1. **Nur lesen.** Alle Systemabfragen sind read-only (`sw_vers`, `csrutil
   status`, `nvram boot-args`, `kextstat`, `ioreg`, `system_profiler`);
   `tests/test_recon.py` prüft diese Liste gegen verbotene Kommandos.
2. **Nur in das Ausgabeverzeichnis schreiben.** Ein Test vergleicht die
   Änderungszeiten aller Repository-Dateien über einen Lauf hinweg.
3. **Kein Apple-Binary.** Die Apple-Stufe liest die Bundles dort, wo sie
   liegen, und gibt Namen, Zahlen, Offsets und Hashes aus — siehe
   [APPLE_DRIVER_ANALYSIS.md](APPLE_DRIVER_ANALYSIS.md) für den rechtlichen
   Rahmen.
4. **Kein Upstream-Code im Repository.** Die Extraktion zitiert Zeilen und
   kürzt den belegenden Quelltext auf eine Zeile pro Fakt.
5. **Keine Rekursion.** Die Stufe `tests` setzt `LB_RECON_IN_SUITE`; ein Lauf
   innerhalb einer Suite startet keine weiteren Suiten.

`tests/test_recon.py` hält das mit 27 Tests fest, und
[`.github/workflows/recon.yml`](../.github/workflows/recon.yml) führt den Lauf
in CI aus und hängt den Bericht als Artefakt an.
