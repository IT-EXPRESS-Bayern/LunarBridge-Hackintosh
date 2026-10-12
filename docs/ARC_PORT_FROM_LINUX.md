<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Arc 130V: den Linux-Treiber portieren, geformt nach dem letzten kompatiblen Apple-Treiber

Stand: 12. Oktober 2026. Dieses Dokument beschreibt die erste Etappe, in der
Hardwareverträge des Linux-Arc-Treibers **portiert** und nicht nur verglichen
werden. Quellen, Hashes und 54 Zeilennachweise stehen in
[ARC_PORT_SOURCES.json](ARC_PORT_SOURCES.json); der Schnittstellenvergleich,
auf dem die Form aufsetzt, in [ARC_INTERFACE_DIFF.md](ARC_INTERFACE_DIFF.md).

## Warum genau diese Richtung

Apples jüngster Intel-Grafiktreiber ist `AppleIntelICLGraphics`, dazu die zwei
SKU-Displaybundles `AppleIntelICLLPGraphicsFramebuffer` und
`AppleIntelICLHPGraphicsFramebuffer` — Ice Lake, Gen11. Für Gen12, Xe oder Xe2
liefert Apple **keinen** Treiber. Es gibt deshalb keinen „letzten kompatiblen
Arc-Treiber" von Apple, den man modifizieren könnte: der letzte kompatible
Apple-Treiber ist eine Generation **ohne** Arc-Hardware, und der einzige
Treiber, der Arc 130V wirklich beschreibt, ist der Linux-Xe-Treiber.

Daraus folgt die Arbeitsteilung dieser Etappe:

* **Form** (was macOS erwartet) kommt vom letzten kompatiblen Apple-Treiber und
  vom Kernelkontrakt: ein `IOService` mit `IOUserClient`, feste geprüfte
  Strukturgrößen, kein Render-Node, kein Syncobj, begrenzte Waits, Speicher
  über `IOMemoryDescriptor`/`IODMACommand`.
* **Inhalt** (was die Hardware verlangt) kommt aus dem Linux-Arc-Treiber:
  PAT-Werte, Registerfolgen, Kontextlayout, Ring- und Deskriptorkodierung.

Portiert wird also der Linux-Treiber, aber in der Gestalt, die der
Apple-Gen11-Treiberpfad vorgibt. Kein Apple-Binary wird gelesen, kein
Linux-Quelltext eingebettet: jeder Wert ist eigener C++-Code mit Nachweis.

## Was in dieser Etappe portiert wurde

| Linux-Stelle | Portiertes Modul | Was die Portierung ändert |
| --- | --- | --- |
| `xe_pat.c`: `xe2_pat_table`, `_PAT_INDEX`, `program_pat`/`program_pat_mcr`, `xe_pat_init_early` | `gpu-prototype/lb_xe2_pat.*` | Aus Funktionszeigern und MMIO-Aufrufen wird ein geordneter Schreibplan mit 33 Einträgen plus Lesekontrolle. Der Plan schreibt nichts; der Aufrufer hält Forcewake, besitzt die BAR und prüft das Ergebnis. |
| `xe_lrc.c`, `regs/xe_lrc_layout.h`, `regs/xe_engine_regs.h`, `xe_vm.c`, `instructions/xe_mi_commands.h` | `gpu-prototype/lb_lrc.*` | Aus einer GEM-Allokation mit `iosys_map`-Zugriffen wird ein Layout plus zehn Kontextimage-Dword-Schreibvorgänge, Ringsteuerwort, PPGTT-Wurzeleintrag, erste Ringinstruktion und Kontextdeskriptor in Speicher, den die Kext selbst besitzt. |
| `xe_lrc.c`: `lrc->desc` | `lb_arc_interface_queue_registration` | Die Anfrage trägt jetzt die **GGTT-Adresse** (`lrcGpuAddress`); den Deskriptor setzt der Port zusammen. Vorher hätte ein Client Privilege- und Addressing-Mode-Bits selbst bestimmen können. |

### PAT: 33 Schreibvorgänge, zwei Bereiche, ein Index

`_PAT_INDEX(i)` ist nicht linear: Indizes 0..7 liegen ab `0x4800`, Indizes
8..31 ab `0x4848`, dazwischen liegen `_PAT_ATS` (`0x47FC`) und `_PAT_PTA`
(`0x4820`). Der Port rechnet beide Bereiche nach und verifiziert sie gegen
`_PICK_EVEN_2RANGES`.

Die 32 Tabellenwerte entstehen aus `XE2_PAT(no_promote, comp_en, l3clos,
l3_policy, l4_policy, coh_mode)` mit `NO_PROMOTE` = Bit 10, `COMP_EN` = Bit 9,
`L3_CLOS` = Bits 7:6, `L3_POLICY` = Bits 5:4, `L4_POLICY` = Bits 3:2,
`COH_MODE` = Bits 1:0. Upstream definiert 28 Einträge; die Indizes 16 bis 19
sind reserviert und werden ausdrücklich mit der Null-Kodierung programmiert
(maximales Caching, minimale Kohärenz). Daraus ergeben sich unter anderem:

| Index | Bedeutung | Wert |
| --- | --- | --- |
| 2 | Write-Back, kohärent (`XE_CACHE_WB`) | `0x00F` |
| 3 | Uncached (`XE_CACHE_NONE`) | `0x03C` |
| 12 | Uncached mit Kompression | `0x23C` |
| 15 | Write-Through (`XE_CACHE_WT`) | `0x614` |
| ATS | `xe2_pat_ats` | `0x00F` |

Index 2 ist genau der Wert, den der [GGTT-Planer](GGTT_BINDING.md) schon als
`LB_GGTT_XE2_WB_PAT_VALUE` kodiert; der Port erzwingt diese Übereinstimmung
jetzt per `static_assert` statt per Kommentar.

**Programmiert heißt nicht nutzbar.** Ein PPGTT-Leaf-Eintrag verteilt fünf
PAT-Bits (3, 4, 7, 62, 61) — daher die 32 Einträge. Ein Verzeichniseintrag
kodiert nur die PAT-Bits 1:0, und ein GGTT-Eintrag trägt ebenfalls nur zwei;
`xe_ggtt.c` setzt dafür ausdrücklich `xe_assert(xe, pat_index <= 3)`. Write-Back
(2) und Uncached (3) sind damit global bindbar, Write-Through (15) und
Uncached-mit-Kompression (12) **nicht**. `lb_xe2_pat_index_spaces()` gibt diese
Erreichbarkeit je Index zurück, der Portbericht trägt `patGgttMaxIndex = 3`,
und der Userspace-Dekoder weist einen Bericht ab, der das anders behauptet.

Eine zweite Korrektur betrifft Forcewake: Im PAT-**Programmierpfad** nimmt
Upstream keinen Wake — jeder `xe_force_wake_get` in `xe_pat.c` steht in einem
Dump-Pfad, weil die Programmierung in der frühen GT-Initialisierung läuft. Die
Wake-Anforderung dieses Ports ist also eine eigene konservative Regel und als
solche im Header vermerkt, keine Upstream-Tatsache.

Weitere portierte Eigenschaften: Der Grafik-GT verlangt Multicast-Schreibzugriffe,
der Media-GT einfache MMIO-Writes. `pat_pta` programmiert Linux nur auf
diskreten Geräten — Lunar Lake ist integriert, ein „discrete"-Aufruf wird
deshalb abgewiesen statt mit einem zusätzlichen Write bedient. Nur
`GRAPHICS_VERx100 == 2004` ist zugelassen: Xe2_HPG 2001 reduziert die Tabelle
über `Wa_16023588340` auf 28 Einträge und braucht seine eigene Qualifikation.

### LRC: Layout, Kontextimage, Deskriptor

| Bereich | Größe | Herkunft |
| --- | --- | --- |
| Ring | Parameter, upstream 16 KiB | `xe_exec_queue.c`, `xe_execlist.c` |
| PPHWSP | 4 KiB | `LRC_PPHWSP_SIZE` |
| Kontextimage | Render 3 × 4 KiB, Compute 2 × 4 KiB | `xe_gt_lrc_size()` für `GRAPHICS_VER >= 20` |
| WA-BB | 4 KiB | `LRC_WA_BB_SIZE`, am Ende der Allokation |

Gesamtgröße ist `ring + lrc_size + LRC_WA_BB_SIZE`; der PPHWSP folgt dem Ring,
weil `xe_lrc_pphwsp_offset()` genau die Ringgröße zurückgibt. Für Render mit
16-KiB-Ring ergibt das 36.864 Byte, für Compute 32.768 Byte.

Geschrieben werden zehn Dwords im Kontextimage, an den Indizes aus
`xe_lrc_layout.h` (jeweils „Registeroffset + 1"):

| Index | Feld | Wert |
| --- | --- | --- |
| 3 | `CTX_CONTEXT_CONTROL` | `0x00090009` (maskiertes Enable von `INHIBIT_SYN_CTX_SWITCH` und `ENGINE_CTX_RESTORE_INHIBIT`) |
| 9 / 5 / 7 | `CTX_RING_START` / `_HEAD` / `_TAIL` | GGTT-Adresse des Rings, 0, Tail |
| 11 | `CTX_RING_CTL` | `(Ringgröße − 4096) \| RING_VALID` |
| 7 | `CTX_RING_TAIL` | 8-Byte-ausgerichtete Spitze, nach dem Prolog 8 |
| 35 / 37 | `CTX_TIMESTAMP` / `_UDW` | 0 (Xe2 hat 64-Bit-Timestamps) |
| 49 / 51 | `CTX_PDP0_UDW` / `_LDW` | PPGTT-Wurzel, Verzeichniskodierung mit PAT 2 |
| 47 | `CTX_ASID` | Adressraumkennung, Xe2 setzt `has_asid` |

Die Ringspitze ist 8-Byte-granular: `RING_TAIL` kodiert sie in `TAIL_ADDR` =
Bits 20:3, Bits 2:0 gehen verloren (`RING_HEAD` ist mit `HEAD_ADDR` = Bits 20:2
dagegen 4-Byte-granular). Upstream löst das in `xe_lrc_write_ring()` mit
`ALIGN(size, 8)` und einem angehängten `MI_NOOP`. Der Ringprolog dieses Ports
besteht deshalb aus **zwei** Dwords — Arbitration-Enable plus NOOP — und der
Builder weist einen nicht 8-Byte-ausgerichteten Tail ab. Eine erste Version
akzeptierte einen 4-Byte-Tail und hätte eine Spitze gesetzt, die das Register
nicht ausdrücken kann.

Der Deskriptor ist `LRC_VALID | (LRC_LEGACY_64B_CONTEXT << 3) | LRC_PRIVILEGE`
plus GGTT-Adresse des PPHWSP, also `Adresse | 0x119`. Engine-Klasse und
-Instanz gehören **nicht** hinein: Upstream setzt sie nur unter
`GRAPHICS_VERx100 < 1250`, Lunar Lake ist 2004. Die erste Ringinstruktion ist
`MI_ARB_ON_OFF | MI_ARB_ENABLE` = `0x04000001`.

**Nicht portiert, mit Begründung:** Indirect Ring State (kein Graphics-Descriptor
setzt `has_indirect_ring_state` an diesem Pin — der Port weist die Variante
ausdrücklich ab), Indirect-Context-Page (`gt_engine_needs_indirect_ctx()` ist
dort konstant falsch), der Inhalt der WA-BB (die Seite wird reserviert, nichts
emittiert), die `set_offsets()`-Registerlisten für Save/Restore, MSI-X-Kontextfelder
sowie PXP, Run-Alone, OA und Parallel-Submission.

## Verifikation

| Nachweis | Umfang |
| --- | --- |
| `tests/test_arc_port.py` | 28 Tests: **jeder** erwartete Wert wird aus den gepinnten Intel-Quellen geparst und gegen das kompilierte Modul geprüft — alle 32 Tabellenwerte, alle 32 Registeroffsets, die Cache-Indizes, Layout, Dword-Offsets, Steuerworte, Deskriptor, PDE und Ringinstruktion; dazu die Bundle-Form des Apple-Vorbilds |
| `tests/test_arc_interface.py` | 52 Tests, einschließlich der vom LRC-Port zusammengesetzten GuC-Registrierung und des Portberichts von Selector 9 |
| `tests/test_interface_diff.py` | 13 Tests des Schnittstellenvergleichs gegen dieselben Pins |
| `tests/test_kernel_build_contract.py` | Kernelflags und Quellenzahl des Kextbuilds, jetzt 16 Kernelquellen |
| `.github/workflows/interface-diff.yml` | Linux-CI: Quellen holen, Module bauen, alle drei Suiten |
| `.github/workflows/native-build.yml` | Intel-macOS-CI: beide Module in der Host-Bibliothek und im Kextbuild |

Ein Transkriptionsfehler im Port fällt damit im Test auf, nicht erst am Gerät:
Die Erwartungswerte stehen nicht im Test, sondern werden aus `xe_pat.c`,
`xe_lrc.c`, `xe_lrc_layout.h`, `xe_engine_regs.h`, `xe_mi_commands.h`,
`xe_instr_defs.h`, `xe_gtt_defs.h`, `xe_vm.c` und `xe_pci.c` gelesen.

## Die Spiegelung des Apple-Bundles: Stand und offene Punkte

Apple teilt die ICL-Generation in **drei** getrennte Kexte unter
`/System/Library/Extensions`: einen Accelerator (`AppleIntelICLGraphics`) und
zwei SKU-Framebuffer (`…ICLLPGraphicsFramebuffer`, `…ICLHPGraphicsFramebuffer`).
Der Mach-O-Name jedes Bundles ist die Bundle-ID ohne das Präfix
`com.apple.driver.Apple`. ICL ist die einzige Generation dieser Liste mit zwei
Framebuffer-Bundles für einen Accelerator.

Diese Kext besetzt genau den Platz der Accelerator-Hälfte: eine Personality
mit `IOClass = LunarBridgePCI` auf `IOProviderClass = IOPCIDevice`,
`IOMatchCategory = LunarBridgeDiagnostic`, und `OSBundleLibraries` nennt
`com.apple.iokit.IOPCIFamily` plus die KPIs — **kein** `IOGraphicsFamily`.

Eine zweite, gespiegelte Framebuffer-Personality bräuchte eine eigene Klasse,
eine eigene Match-Kategorie, die zusätzliche `IOGraphicsFamily`-Abhängigkeit
und eine `IOFramebuffer`-Unterklasse mit ihren **acht** rein virtuellen
Methoden. Der veröffentlichte Header ist dafür nicht ausreichend: seine eigenen
Includes (`IOGraphicsDevice.h`, `IOFramebufferShared.h`,
`IOFramebufferPrivate.h`) sind nicht öffentlich. Eine solche Unterklasse ließe
sich aus veröffentlichtem Material also nicht einmal übersetzen. Die
Displayhälfte bleibt deshalb bewusst unbesetzt, statt eine Personality
anzumelden, die einen Displaypfad behauptet, den sie nicht bedienen kann.
Drei Tests in `tests/test_arc_port.py` halten diesen Stand fest: die
ICL-Dreiteilung, die acht nicht erfüllbaren Methoden und die eine Personality
ohne Display-Abhängigkeit.

## Beobachtbar am Gerät

Der Port wäre sonst toter Code in der Kext. Selector 9 veröffentlicht deshalb
einen 112-Byte-Bericht: PAT-Eintragszahl und Schreibanzahl, ATS-Offset und
-Wert, die vier Cache-Indizes, Ringgröße, Kontext- und Ringsteuerwort,
Imageseiten und Gesamtgrößen für Render und Compute sowie die
Deskriptorvorlage `0x119`. `tools/collect_macos.py` liest ihn als
`arc_port_plan` mit, `tools/lb_wire.py` weist einen Bericht ab, der von den
Portwerten abweicht oder programmierte Hardware behauptet, und Trace-Ereignis
63 hält die Abfrage fest. Damit lässt sich an der geladenen Kext prüfen, was
sie programmieren *würde* — und später gegen eine echte Lesekontrolle stellen.

## Grenzen

Nichts davon programmiert Hardware. Der PAT-Plan hat kein Register geschrieben,
das Kontextimage liegt in CPU-Speicher, kein GGTT-Eintrag ist gesetzt, keine
Firmware authentifiziert, keine Queue registriert, kein Fence abgeschlossen.
Jede erzeugte Struktur meldet ihre fehlenden Qualifikationen; `hardwareBound`,
`gpuExecuted` und `metalVerified` bleiben 0. Die Kext lädt weiterhin nicht
nachweisbar, und ein Metal-Gerät existiert nicht.

## Nächste Schritte

1. Selector für eine PAT-Lesekontrolle: 33 Register unter dem bestehenden
   MMIO-Opt-in lesen und mit `lb_xe2_pat_check_readback` vergleichen. Das ist
   die erste Messung, die den Port am Zielgerät bestätigen oder widerlegen kann.
2. LRC-Allokation an den Geräte-DMA-Mapper und den GGTT-Planer binden, damit
   die GGTT-Adressen im Deskriptor echte Reservierungen sind.
3. PPGTT-Wurzel wirklich aufbauen (vier Ebenen, `va_bits = 48`,
   `vm_max_level = 4` aus dem Xe2-Feature-Set) und den Wurzeleintrag aus der
   echten Tabelle statt aus einer Testadresse bilden.
4. GuC-Ladefolge portieren (DMA-Register, Statuspolling, Authentifizierung),
   dann Registrierung und Submit mit echtem Fenceabschluss.
5. Erst danach die Display- und Metal-Seite, für die der öffentliche
   IOAccel-Vertrag nicht ausreicht.
