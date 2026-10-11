# Xe2: geprüfter GGTT-Schreibplan aus DMA-Seiten

`lb_ggtt_build_plan` verbindet die bereits vorhandene DMA-/PPGTT-Prüfung mit einer
separaten GGTT-Reservierung. Die Ausgabe ist eine begrenzte Liste aus BAR-Offset
und 64-Bit-GGTT-PTE je 4-KiB-Seite. Der tatsächliche C++-Code wurde auf Windows
ausgeführt und als freistehendes x86_64-macOS-Objekt kompiliert. Es erfolgten keine
BAR-Schreibzugriffe, keine PAT-Programmierung und keine native GPU-Ausführung.

Die Funktion steht in `gpu-prototype/lb_ggtt_plan.h/.cpp`; die Hosttests stehen in
`tests/test_ggtt_plan.py`. Eigener Code: GPL-3.0-only. Gepinnte Primärstellen,
Dateihashes und Fremdlizenzen: `GGTT_BINDING_SOURCES.json`.

## Eingaben und Ablauf

Der spätere Kernelaufrufer hält zwei **getrennte Arena-Objekte** und deren
Reservierungen: den Quelladressraum der DMA-/PPGTT-Seiten und den globalen
GGTT-Adressraum. Beide Tokens müssen noch gültig sein und demselben angegebenen
Eigentümer gehören. Identische numerische Adressen in verschiedenen Adressräumen
sind zulässig; identische oder überlappende Arena-Objekte werden abgelehnt. Die
GGTT-Arena muss als Ganzes in ihrem ausdrücklich reservierten globalen Fenster
liegen. Diese Metadatenprüfung ersetzt keinen realen globalen GPU-Allocator.

1. Der bestehende `lb_vm_stage_ptes` prüft die Quellreservierung und sämtliche
   DMA-Segmente: exakte Gesamtlänge, 4-KiB-Ausrichtung, 46-Bit-DMA-Grenze,
   fehlende Segmentüberlappung, Eigentümer, Token und Arena-Konsistenz.
2. Jedes angelieferte PPGTT-PTE muss bytegenau dem neu berechneten Quell-PTE
   entsprechen. Abweichende Adresse, Flags, Reihenfolge oder Anzahl brechen ab.
3. Dieselbe Core-Prüfung validiert die **separate** GGTT-Reservierung gegen
   dieselben DMA-Seiten. Größen, Schreibrecht und Cachepolitik müssen passen.
4. Der Planer prüft die GGTT-ABI, GSM- und BAR-Längen, den unteren reservierten
   Bereich, die GuC-Obergrenze und alle PTE-Indizes/Schreiboffsets.
5. Die DMA-Segmente werden in Originalreihenfolge in einzelne Seiten zerlegt.
   `lb_xe2_page_entry(LBGgtt4K, ...)` kodiert daraus neue GGTT-PTEs. Erst nach
   erfolgreicher Gesamtprüfung wird die vollständige Ausgabe übernommen.

Maximal 256 Seiten beziehungsweise 1 MiB werden zu maximal 256 Schreibzeilen.
Unbenutzte Zeilen und reservierte Felder der erfolgreichen Ausgabe sind null.
Bei jedem Fehler bleiben **alle Ausgabebytes und Eingaben unverändert**. Die
Übernahme ist eine logische Transaktion unter Aufruferserialisierung; sie ist
kein atomarer 4208-Byte-Prozessorstore und bietet keine Lock-free-Semantik.

## Format und Cachevertrag

GGTT-Einträge sind 64 Bit breit und adressieren 4-KiB-Seiten. Xe2 verwendet für
GGTT-PAT die Bits 52 und 53; Bit 1 bedeutet lokalen Gerätespeicher. Es ist hier
kein Schreibrecht-Bit wie im PPGTT. Daher werden PPGTT-Einträge vollständig
überprüft und anschließend unabhängig neu kodiert. Der gepinnte Xe-Pfad setzt
GSM für diese Generation auf 8 MiB und adressiert es ab BAR-Offset 8 MiB.
Die nutzbare GGTT-Obergrenze ist `0xFEE00000`.
[GGTT-Implementierung](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_ggtt.c),
[PTE-Definitionen](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/regs/xe_gtt_defs.h).

Dieser enge erste Plan akzeptiert ausschließlich schreibbare **System-DMA-Seiten
mit PAT-Index 2** in beiden Reservierungen. Die gepinnte Xe2-Tabelle ordnet WB
Index 2 zu; dessen PAT-Wert ist `XE2_PAT(0,0,0,0,3,3)` = `0x0f` mit Kohärenzmodus
3, ohne Kompression. Ein Planwert bestätigt noch keine PAT-Register auf dem
Laptop. PAT0 oder ein Read-only-Quellmapping werden ausdrücklich abgelehnt,
anstatt Cachepolitik oder Rechte umzudeuten.
[Xe2-PAT-Tabelle](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_pat.c).

Der untere eigene Planungsbereich bleibt mindestens **8 MiB** frei. Dies ist eine
bewusst konservative Projektentscheidung anhand von `MAX_WOPCM_SIZE`; der
gepinnten integrierten LNL-Standardgröße entsprechen hingegen 2 MiB. Die acht
MiB sind kein ausgelesener BIOS-Wert. Ein höherer, seitenausgerichteter unterer
Reservewert ist zulässig und begrenzt dann die gesamte GGTT-Arena.
[WOPCM-Größen und gesperrte BIOS-Konfiguration](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_wopcm.c).

Für ein System-DMA-PTE lautet die überprüfte Kodierung:

```text
GGTT-PTE     = DMA-Seitenadresse | 1 | (1 << 53)
PTE-Index    = GGTT-Adresse / 4096
BAR-Offset   = 8 MiB + PTE-Index * 8
Schreibende  = erster BAR-Offset + Seitenanzahl * 8  (exklusiv)
```

Die Adresse muss aus dem Mapper **dieses PCI-Geräts** stammen. Lunar Lake hat in
der gepinnten Xe-Plattformdefinition eine DMA-Maske von 46 Bit.
[LNL-Plattform](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_pci.c).

## C-ABI und Fehler

Die 64-Bit-ABI ist versioniert mit `LB_GGTT_PLAN_ABI = 1`. Alle Puffer brauchen
mindestens 8-Byte-Ausrichtung.

| Typ | Größe | Zweck |
| --- | ---: | --- |
| `LBGgttWindow` | 72 Byte | BAR-/GSM-Längen, GGTT-Grenzen, PAT-Vertrag, ABI |
| `LBGgttWrite` | 16 Byte | BAR-Byteoffset und 64-Bit-PTE |
| `LBGgttPlan` | 4208 Byte | Reservierungsdaten, Prüfgrenzen und bis zu 256 Zeilen |

Status 0 bedeutet **gültiger CPU-Plan**. Status 1 bis 7 werden unverändert vom
bestehenden Core übernommen: Input, Alignment, Range, Capacity, Owner, NotFound,
Corrupt. Neue Statuswerte sind 8 = PteMismatch, 9 = WindowInvalid, 10 = Policy,
11 = Aliasing. Die Ausgabe muss genau `sizeof(LBGgttPlan)` groß sein und darf
keine Eingabe überlappen. Der Planer kontrolliert Pointerarithmetik und
angegebene Längen; er kann nicht prüfen, ob eine vom Kernelaufrufer deklarierte
Speicheradresse tatsächlich so viel allokierten Speicher enthält.

Der Kernelaufrufer hält gültige, stabile Puffer, serialisiert Arenaänderungen und
hält Memory-Descriptor, PCI-Geräte-Mapper und vorbereiteten DMA-Command für die
gesamte Nutzung zurück. Rohe UserClient-Daten oder vom Nutzer erfundene DMA-
Adressen gehören nicht an diese API. Die Funktion allokiert nicht und verwendet
begrenzte lokale Stagingdaten: 2048 Byte PTE-Prüfpuffer plus 4208 Byte Plan und
kleine lokale Werte. Der spätere Kernelaufrufer muss sein Stackbudget beachten.

## Produktionsgrenze

Der vorhandene `native/LBDMAContext.cpp` erzeugt gerätespezifische
`IODMACommand::kMapped`-/Host64-Segmente. Seine Arena, Segmente und PTEs sind
jedoch privat; er exportiert bisher keinen stabilen Snapshot für diesen Planer.
Sein vorhandenes PAT0-PPGTT-Staging erfüllt den hier ausdrücklich geforderten
PAT2-Vertrag nicht. Ein späterer Adapter muss einen qualifizierten Cachevertrag,
Ressourcenlebensdauer und eine echte separate globale GGTT-Reservierung liefern.
Es wurde dafür keine Änderung am bestehenden DMA-Adapter vorgenommen.

Apples Schnittstellen sehen einen Geräte-Mapper über `copyMapperForDevice` und
vorbereitete `gen64IOVMSegments` vor. `prepare` hält Mappingressourcen, `complete`
gibt sie frei; Mappingressourcen dürfen nicht vor Ende der GPU-Nutzung beendet
werden. Ein physischer oder System-Mapper-Ersatz würde den Gerätebezug aufheben.
[IOMapper](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/iokit/IOKit/IOMapper.h),
[IODMACommand](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/iokit/IOKit/IODMACommand.h).

Jeder erfolgreiche Plan führt `requiredQualifications = 127` und lässt
`hardwareBound`, `flushCompleted`, `globalOwnershipVerified` **dauerhaft 0**.
Der spätere native Binder muss selbst nachweisen:

| Bit | Noch benötigte Qualifikation |
| ---: | --- |
| 0 | Aktuelle DMA-Seiten aus dem exakten Geräte-Mapper, Ressourcen gehalten |
| 1 | Tatsächliche globale GGTT-Ownership einschließlich bestehender Systemnutzer |
| 2 | Passendes PCI-Gerät, qualifiziertes und ausreichend langes BAR/GSM-Mapping |
| 3 | Wirklich programmierter, passender Xe2-PAT-/Kohärenzvertrag |
| 4 | Tatsächliche WOPCM-/Firmware-/Scratch-Reservierungen |
| 5 | Korrekte native PTE-Schreiboperation und generationsabhängige Workarounds |
| 6 | Schreibbarriere und abgeschlossene GGTT-TLB-Invalidierung aller relevanten GTs |

Die Referenz invalidiert GGTT nach Schreibzugriffen, einschließlich einer
Schreibbarriere und Graphics-/Media-GT-TLB-Behandlung. Das erzeugte Datenformat
führt diese Hardwareoperationen nicht aus.
[GGTT-Barrier-/TLB-Pfad](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_ggtt.c#L434-L461).

## Nachweis und Wiederholung

31 Hosttests bestanden mit 132 Aufrufen des tatsächlichen C++-Planers, ohne
Fehler oder übersprungene Tests. Sie prüfen die Zusammenführung, fragmentierte
und mehrseitige DMA-Segmente, unabhängig gültige Quell-/GGTT-Tokens, die letzte
zulässige GGTT-Seite, PAT-/Rechteablehnung, manipulierte Quell-PTEs, BAR-Grenzen,
Pointerarithmetik, Aliasing und die vollständige Ausgabeerhaltung bei Fehlern.
Die Tests verwenden eigene synthetische Metadaten, keine nachgebildete
GGTT-Implementierung. Die bestehenden einzelnen Core-PTE-/Mappingtests bleiben
separat. Das freistehende x86_64-macOS-Objekt wurde unter Windows erzeugt; dies
ist ein Compile-Nachweis und keine auf macOS geladene oder ausgeführte Kext.

Nach Einbindung beider C++-Dateien in die eigene Hostbibliothek:

```text
python tests/test_ggtt_plan.py --library <eigene Hostbibliothek> --report <lokaler JSON-Bericht>
```

Der optionale Bericht enthält Quell-/Bibliothekshashes, Testzahl und explizite
Grenzen. Absolute private Pfade, Nutzerkennung und Geräte-UUIDs werden nicht
ausgegeben. Native Speicherbindung, GPU-Submission, Metal und die tatsächliche
Installation auf dem Samsung bleiben ungetestet.

## Fremdrechte der Formatreferenzen

Die Zahlen-/Formatreferenzen in den neuen C++-Dateien stammen aus MIT-gekennzeichneten
Intel-Xe-Primärstellen: Copyright © 2021, 2022, 2023, 2024 Intel Corporation.
Die Funktion wurde eigenständig geschrieben; Linux-Treiberimplementierungen
und Apple-Header werden nicht als Quellcode dieses Moduls ausgeliefert. Die
Apple-Schnittstellen dienen ausschließlich als Referenzen unter APSL-2.0.

Der vollständige MIT-Lizenztext ist außerdem in `LICENSES/MIT-Intel.txt` enthalten:

```text
Copyright © 2021, 2022, 2023, 2024 Intel Corporation

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
```
