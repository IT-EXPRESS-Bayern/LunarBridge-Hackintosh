# Eigener Xe2-Kernel: CPU-Payload und Relokationsvertrag

`own_kernel_payload.py` bereitet die Startdaten unseres unveränderten eigenen
OpenCL-Kernels vor. Es prüft zuerst dessen vollständigen SHA256 und nutzt den
vorhandenen begrenzten Zebin-Parser. Es erzeugt ausschließlich CPU-Bytes und
meldet beide Pufferbindungen als offen. Es lädt keine Compute-Laufzeit und
enthält keinen GPU-Aufruf. **Native Submission und Metal-Unterstützung fehlen
weiterhin.** Die historischen Fixtures und der Windows-Hardware-Nachweis werden
nicht verändert.

## Tatsächlicher Datenvertrag

Der eigene Kernel trägt ZEInfo 1.73, SIMD32, 128 GRF und 32 Inline-Bytes. Seine
Metadaten enthalten diese fünf Cross-Thread-Felder:

| Feld | Offset im vollständigen Payload | Größe | Inhalt im Standardplan |
| --- | ---: | ---: | --- |
| `global_id_offset` | 0 | 12 | `0, 0, 0` |
| `enqueued_local_size` | 12 | 12 | `32, 1, 1` |
| Eingabepuffer, Argument 0 | 24 | 8 | Null, noch keine GPU-VA |
| Ausgabepuffer, Argument 1 | 32 | 8 | Null, noch keine GPU-VA |
| `count`, Argument 2 | 40 | 4 | 64 |

Der [Intel-ZEInfo-Decoder](https://github.com/intel/compute-runtime/blob/7323fcafa8979708e73f1865c61cb9790b367948/shared/source/device_binary_format/zebin/zeinfo_decoder.cpp#L1155)
rundet das letzte Argumentende 44 auf 32 Byte auf: **64 Cross-Thread-Bytes**.
Die letzten 20 Byte bleiben Null. Das ist keine frei erfundene GRF-Payloadgröße.

Der gepinnte [Xe2-COMPUTE_WALKER-Header](https://github.com/intel/compute-runtime/blob/7323fcafa8979708e73f1865c61cb9790b367948/shared/source/generated/xe2_hpg_core/hw_cmds_generated_xe2_hpg_core.inl#L5112)
definiert acht Inline-DWORDs, also 32 Byte. Der
[Dispatch-Encoder](https://github.com/intel/compute-runtime/blob/7323fcafa8979708e73f1865c61cb9790b367948/shared/source/command_container/command_encoder_xehp_and_later.inl#L243)
kopiert den vorderen Teil dorthin und verschiebt die verbleibenden Cross-Thread-Daten.
Damit liegt der Input-Pointer inline bei 24, der Output-Pointer indirekt bei 0
und `count` indirekt bei 8. Der
[KernelDescriptor](https://github.com/intel/compute-runtime/blob/7323fcafa8979708e73f1865c61cb9790b367948/shared/source/kernel/kernel_descriptor.h#L65)
ergibt für den anschließenden Per-Thread-Bereich den Offset 32 nach Inline-Abzug.
Diese Aufteilung setzt Inline-Programmierung voraus; ein späterer nativer
Command-Encoder muss diese Entscheidung tatsächlich herstellen.

## Die zwei ISA-Relokationen

Das eigene 896-Byte-Textsegment enthält genau zwei `R_ZE_SYM_ADDR_32`-
Relokationen bei 76 und 156 für `__INTEL_PATCH_CROSS_THREAD_OFFSET_OFF_R0`.
Beide ursprünglichen DWORDs sind Null. Der
[Intel-Linker-Header](https://github.com/intel/compute-runtime/blob/7323fcafa8979708e73f1865c61cb9790b367948/shared/source/compiler_interface/linker.h#L23)
nennt diesen Namen ausdrücklich `implicitArgsRelocationSymbolName`.

Der [Intel-Linker](https://github.com/intel/compute-runtime/blob/7323fcafa8979708e73f1865c61cb9790b367948/shared/source/compiler_interface/linker.cpp#L692)
setzt hier die ausgerichtete Implicit-Args-Strukturgröße, wenn diese benötigt
wird. Stack-Calls oder ein Debugger für ein User-Modul können das aktivieren.
Das eigene authentifizierte ZEInfo fordert weder `require_iab` noch
`has_stack_calls`; deren fehlende Werte sind im
[ZEInfo-Header](https://github.com/intel/compute-runtime/blob/7323fcafa8979708e73f1865c61cb9790b367948/shared/source/device_binary_format/zebin/zeinfo.h#L417)
standardmäßig falsch.

Der neue Baustein unterstützt ausschließlich diesen Fall ohne Implicit-Args,
Stack-Calls und Debugger. Deshalb bleibt an beiden Relokationen der
**ursprüngliche Wert 0** erhalten, und die ISA bleibt bytegleich. Es wird dort
weder 64 noch 32 oder eine GPU-Adresse eingetragen. Alle anderen Policies,
zusätzliche Relokationen, andere Symbole, abweichende Typen, Addends oder
geänderte Binärbytes werden abgelehnt. Dies ist ein enger Linkvertrag für einen
eigenen Kernel, kein allgemeiner Intel-Linker.

## Native Metal-Compilerroute

| Kandidat | Primärquelle und Lizenz | Beitrag zur nativen Route / offene Grenze |
| --- | --- | --- |
| Apple `metal` / AIR | [Apple Metal Tools](https://developer.apple.com/library/archive/documentation/Miscellaneous/Conceptual/MetalProgrammingGuide/Dev-Technique/Dev-Technique.html), Apple-Werkzeugkette | Kompiliert MSL in AIR. Liefert uns dadurch keinen nativen Xe2-Treiber. Hier wurde kein AIR erzeugt. |
| `metal2vulkan` | [README @ 43c46ac8](https://github.com/steelbrain/metal2vulkan/blob/43c46ac8a24adf1a6e872b8a52c706ec9614fad0/README.md), LGPL-3.0-or-later | Alpha: AIR/LLVM-IR → Vulkan-1.2-Shader-SPIR-V. Rust ≥1.87, für AIR-Bitcode `llvm-dis`, außerdem `spirv-val`. Als Translator-Kandidat im nativen Userspace denkbar; keine macOS-Hardware-Vulkan-Laufzeit enthalten. Hier weder gebaut noch ausgeführt. |
| Mesa ANV/NIR/Intel-Compiler | [ANV](https://docs.mesa3d.org/drivers/anv.html), [NIR](https://docs.mesa3d.org/nir/index.html), [Lizenzen](https://docs.mesa3d.org/license.html) | Vulkan-SPIR-V → NIR → Intel-Code ist die Architekturroute aus GPU_DESIGN. XNU-Backend, native Geräteintegration und Build-Port fehlen. Überwiegend MIT; konkrete Dateien haben eigene SPDX-Angaben. Kein Mesa-Code wurde übernommen. |
| Intel IGC | [README @ 6f81427a](https://github.com/intel/intel-graphics-compiler/blob/6f81427a0be7c7c017d5be3f049dabec393cf788/README.md), MIT | LLVM/OpenCL-Compiler, mit OpenCL-Clang und SPIRV-LLVM-Translator. Unser Referenzbinary ist OpenCL-Zebin. Vulkan-Shader-SPIR-V ist kein austauschbarer Inputvertrag hierfür. Kein nachgewiesener Darwin-Build und kein AIR/MSL-Frontend. |
| SPIRV-Cross | [README @ aa217aeb](https://github.com/KhronosGroup/SPIRV-Cross/blob/aa217aeb6c9f0ace7a0ab233b28807edf45eb165/README.md), Apache-2.0; README CC-BY-4.0 | Übersetzt SPIR-V unter anderem nach MSL. Es stellt nicht automatisch die benötigte Gegenrichtung AIR/MSL → Xe2 bereit. |
| Diligent Core | [README @ 074060cf](https://github.com/DiligentGraphics/DiligentCore/blob/074060cfd1db73064d7a18cf40df86af6531520c/README.md), öffentliches Core Apache-2.0 | Anwendungsseitige Grafikabstraktion. Metal-Backend laut README für kommerzielle Kunden verfügbar. Kein angebotener Ersatz für einen fehlenden macOS-Hardwaretreiber. |
| Reims VGPU | [README @ 69a57dd6](https://github.com/steelbrain/reims-vgpu/blob/69a57dd69a6958e946c03b73e02db331f330f435/README.md), LGPL-3.0-or-later | Virtuelle GPU für macOS-Gäste und QEMU-Hostbackend. Das ist nicht die geforderte native Installation. Nur Vergleichsquelle; hier kein VM-Pfad eingerichtet. |

Die nächste native Compilerqualifikation benötigt einen eigenen MSL-/AIR-
Testfall, dessen Übersetzung und einen passenden Vulkan-SPIR-V/NIR/Xe2-
Codegenerator. Das vorliegende OpenCL-Payloadformat darf nicht ungeprüft für
AIR/Vulkan-Shader wiederverwendet werden. Der CPU-Baustein hilft beim ersten
nativen eigenen Compute-Kernel, während die Metal-Route weiter entwickelt wird.
Die genannten externen Projekte bleiben externe Kandidaten mit ihren jeweiligen
Lizenzen; dieses Modul linkt oder verteilt keine ihrer Bibliotheken.

## Nutzung und Prüfung

Vom Repository-Root:

```sh
python gpu-toolchain/own_kernel_payload.py
python gpu-toolchain/own_kernel_payload.py --count 65 --local-size 32
python gpu-toolchain/own_kernel_payload.py --output-dir gpu-toolchain/run-output
python -m unittest discover -s gpu-toolchain/tests -p test_own_kernel_payload.py -v
```

Ohne `--output-dir` wird nur ein pfadfreier JSON-Bericht angezeigt. Mit der
Option wird ausschließlich ein **neues** Verzeichnis mit Cross-Thread-, Inline-,
indirekten und bytegleichen ISA-Dateien plus Bericht erstellt. Vorhandene
Verzeichnisse werden nicht überschrieben. Diese Dateien sind CPU-Vorbereitung
und kein installierbarer Treiber oder ausführbarer GPU-Auftrag.

Der Standardplan verwendet 64 Werte und eine **geplante** lokale Größe 32.
Der historische Windows-Aufruf hat die lokale Größe dem Treiber überlassen;
seine tatsächlich gewählte Größe wurde nicht ausgelesen. Die geplanten Werte
ersetzen diesen unbekannten historischen Wert nicht. Der Baustein plant nur
vollständige 1-D-SIMD32-Gruppen und maximal 4096 Werte; das ist ein eigenes
Vorbereitungsbudget. Hardwaregrenzen der lokalen Größe müssen später separat
qualifiziert werden. Aufgerundete Gruppen sind für den eigenen Kernel durch
seine Prüfung `index < count` abgesichert.

Noch erforderlich: native GPU-VA/DMA-Bindung, Per-Thread-Local-ID- und
Entrypoint-Entscheidung, ISA-/indirekter Heap, WALKER, Queue/GuC, Fence und
Synchronisierung. Der Bericht setzt `dispatch_ready`, native Bindings,
Submission und Metal-Nachweis ausdrücklich auf falsch. Die 64-Byte-
Per-Thread-Metadaten und der Skip-Offset 128 werden nur dokumentiert; daraus
wird keine ungeprüfte Entrypoint-Auswahl abgeleitet.

Die neuen **37 CPU-Tests** prüfen exakte Datenbytes, Null-Padding, beide offenen
Bindings, unveränderte ISA und Relokationswerte, manipulierte Binärdaten,
Ganzzahl-/Policy-/Budgetgrenzen, falsche Relokationen, fehlende Adressen,
Exportintegrität und Fehlerberichte ohne private Pfade. Die historischen
37 Offline-Tests bleiben eine separate Regression. Konkrete Hashes stehen in
[KERNEL_PAYLOAD_VALIDATION.json](KERNEL_PAYLOAD_VALIDATION.json), gepinnte
Primärdateien in [KERNEL_PAYLOAD_SOURCES.json](KERNEL_PAYLOAD_SOURCES.json).

Eigener Code: GPL-3.0-only, siehe [Projektlizenz](../LICENSE).
Die neu ausgewerteten Intel-Header/Definitionen tragen MIT, siehe
[ergänzende Attribution](KERNEL_PAYLOAD_NOTICE.txt) und [MIT-Text](NOTICE).

Der Export akzeptiert ausschließlich den exakten Datentyp und erzeugt Dateien und Bericht aus einer frisch authentifizierten Vorlage. Abgeleitete Klassen können die Prüfung und die Ausführungsnachweise nicht überschreiben. Der unabhängige Reviewfall ist als Regressionstest enthalten.
