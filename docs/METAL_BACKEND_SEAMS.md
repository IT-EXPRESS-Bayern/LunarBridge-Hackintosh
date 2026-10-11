<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Metal-Shader-SPIR-V zu nativem Xe2-Code: konkrete Compileranschlüsse

Mesa enthält einen CPU-Compiler für Lunar Lake. Der nutzbare Anschluss ist
`spirv_to_nir` → Vulkan-Descriptor-/Push-Lowering → `brw_compile` mit einem
Compute-Shader. Das Ergebnis ist Intel-ISA mit `brw_cs_prog_data` und einer
Bindungsmap. **Es ist kein OpenCL-Zebin und besitzt nicht dessen Argumentlayout.**
Ein eigener nativer Userspace-Compiler ist damit aus freien Quellen grundsätzlich
implementierbar. Seine Portierung und ein echter Shader-ISA-Compilelauf stehen
noch aus; dieses Dokument qualifiziert die Quellen und den nächsten Bauweg.

## Quellenstand und tatsächlich erhobene Belege

Alle nachfolgenden Codeangaben beziehen sich auf Mesa-Commit
`e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae`, dessen `VERSION` den Wert
`26.2.0-devel` enthält. Mesa benennt sein
[offizielles GitLab-Repository](https://docs.mesa3d.org/repository.html).
Die gelesenen Originaldateien wurden wegen nicht erreichbarer GitLab-/Archiv-
Transfers über den [GitHub-Transportmirror](https://github.com/mirror/mesa/tree/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae)
bezogen. Dieser Transport ist kein separater Treiber und kein authentifiziertes
Release. Bei **54 unterschiedlichen Dateien / 1.871.758 Byte** wurden jeweils
Git-Blob-SHA1 einschließlich des Git-Objektheaders und SHA256 aus den tatsächlichen
Dateibytes neu berechnet und mit den gelieferten Blobwerten verglichen. Eine
Upstream-Signatur oder Übereinstimmung mit einem vollständigen Releasearchiv
wurde dadurch nicht geprüft.

Das separat veröffentlichte
[Mesa-26.2.4-Archiv](https://docs.mesa3d.org/relnotes/26.2.4.html) trägt den
offiziellen SHA256
`bce5f7fbebb934373b86c999a064d52fb5065878dc57f287f95346648ec832e9`.
Der Download wurde innerhalb seiner Frist nicht vollständig; diese Release-
Prüfsumme ist deshalb **kein Nachweis für die hier untersuchten Dateien**.
Ein Releasewechsel verlangt eine neue Quellen- und ABI-Prüfung.

Erhoben wurden Quellen-/Hashprüfungen und eine XML-Prüfung der Xe2-Paketfelder.
Es wurden in dieser Etappe weder Mesa konfiguriert/gebaut noch ein Shader mit
Mesa übersetzt oder auf der Arc 130V ausgeführt. Die bestehenden Metal- und
OpenCL-Proben behalten ihre jeweils eigenen Build-/Runtime-Belege.

## Tatsächliche CPU-Einstiege und erforderliche Vorarbeiten

| Stufe | Datei / Funktion am Pin | Benötigter Vertrag |
| --- | --- | --- |
| Vulkan-SPIR-V → NIR | `src/compiler/spirv/nir_spirv.h:165`, `spirv_to_nir` | DWORD-Anzahl statt Byteanzahl, `MESA_SHADER_COMPUTE`, Entry-Name, `NIR_SPIRV_VULKAN`, geprüfte Capabilities, Addressformate und die Compute-`nir_options` des Intel-Compilers. |
| Intel-Compiler anlegen | `src/intel/compiler/brw/brw_compiler.h:1273`, `brw_compiler_create`; Implementierung in `brw_compiler.c:83` | Lebensdauer von `intel_device_info` und Ralloc-Kontext, generierte Device-/Workaround-Daten, ISA-/Registersets. Kein Geräte-Open nötig für die statische PCI-Konfiguration. |
| NIR-Vorverarbeitung | `src/intel/compiler/brw/brw_nir.h:139`, `brw_preprocess_nir` | Typ-/SSA-/Systemvalue-/Compute-Lowering. Der ANV-Aufruf steht in `anv_shader_compile.c:334–378`. Dies ersetzt kein Descriptor-Lowering. |
| Speicherbindungen senken | `src/intel/vulkan/anv_nir.h`, `anv_nir_apply_pipeline_layout` und Addressformat-Helfer | Tatsächliche Set-/Bindinglayouts, Robustness, Descriptorlayout, `anv_pipeline_bind_map` und temporäre `anv_pipeline_push_map`. |
| Compute/Payload senken | `brw_nir_lower_cs_intrinsics`; `anv_nir_lower_driver_values`; `anv_nir_compute_push_layout`; `anv_nir_lower_resource_intel` | Local IDs, Dispatch-/Descriptor-Driverwerte, endgültige Push-Ranges/Inline-Selektoren und vollständig gesenkte Resource-Zugriffe. Reihenfolge in `anv_shader_lower_nir`, `anv_shader_compile.c:1247–1572`. |
| NIR → Xe2-ISA | `brw_compiler.h:1298–1330`, `brw_compile_params` / `brw_compile`; `brw_shader.cpp:1054`, Compute-Zweig → `brw_compile_cs` | `nir`, gültiger Compute-Key, `brw_cs_prog_data`, `mem_ctx`, Fehler-/Statistikdaten. Nicht-null Assembly und `program_size` sind tatsächliche Compileroutputs. |
| Paket-/Dispatchdaten | `brw_cs_get_dispatch_info`; `genX_shader.c:1146`; `genX_cmd_compute.c:434–603` | SIMD, Threads, Maske, Descriptor-/Inline-Daten und später gebundene GPU-Adressen. Diese Stufe enthält Runtime-Anteile und gehört nicht unverändert in einen CPU-Compiler. |

Die zugehörigen Primärdateien sind
[SPIR-V-API](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/compiler/spirv/nir_spirv.h),
[Intel-Compiler-API](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/compiler/brw/brw_compiler.h),
[ANV-Compilefolge](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/vulkan/anv_shader_compile.c)
und [ANV-NIR-APIs](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/vulkan/anv_nir.h).
Diese sind interne APIs am genannten Pin, keine stabile macOS-Treiber-ABI.

`anv_shader_lower_nir` nimmt eine echte `anv_device`-Umgebung mit PhysicalDevice,
ISL-/Descriptor-Konfiguration und Layoutobjekten an. Ein leerer Dummy dieser
Strukturen reicht nicht. Der Compileradapter muss die benötigten reinen
Lowering-Passes mit einem vollständig definierten Layout auskoppeln oder einen
gleichwertigen engen NIR-Speichervertrag implementieren und gegen ANV prüfen.
Der aktuelle Pin nutzt standardmäßig BRW: `intel_debug.c:313–322` initialisiert
`INTEL_JAY`/`use_jay` mit 0; Jay ist eine auswählbare Alternative für `ver == 20`.
Ein Qualifikationslauf muss diese Compilerwahl explizit festhalten und fremde
Debug-/Shader-Replacement-Umgebungen ausschließen.

## Lunar Lake: offline kompilierbar, reale Topologie separat

`include/pci_ids/iris_pci_ids.h:260–262` ordnet `0x64A0` Lunar Lake zu.
`intel_device_info.c:1149–1220` ergibt `ver = 20`, `verx10 = 200`,
`grf_size = 64` und `has_local_mem = false`.
`intel_get_device_info_from_pci_id(0x64A0, ...)` liefert eine statische
Compilerkonfiguration ohne DRM-Gerät. Diese Mesa-Versionsskala unterscheidet
sich von Linux-Xe `graphics_verx100 = 2004`.
[PCI-/Device-Quelle](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/dev/intel_device_info.c)

Die statische Konfiguration enthält ausdrücklich Platzhalter für Threads/URB.
Revisions-/Fuse-/EU-/Subslice-/Threadgrenzen stammen beim Linux-Treiber aus den
KMD-/HWConfig-Abfragen unter `src/intel/dev/xe/intel_device_info.c`.
Ein späterer XNU-Gerätevertrag muss sie auf der tatsächlichen Arc 130V erheben.
Ein statischer Compilerlauf beweist weder diese Topologie noch einen sicheren
CFE-/Scratch-/SLM-Dispatch.

## Shader- und Payloadvertrag für den ersten eigenen Kernel

Unser [MSL-Testkernel](../gpu-toolchain/xe2_compute_probe.metal) verwendet
64×1×1 lokale Threads und drei Bufferbindungen: Input 0, Output 1 und einen
4-Byte-Count-Buffer 2. Der vom AIR-Translator erhaltene Vulkan-Shader muss diese
Bindungen anhand seiner tatsächlichen Reflection tragen. Der OpenCL-Zebin-
Count-Scalar bei Offset 40 ist kein Ersatz für diesen dritten Buffer.

`anv_nir_apply_pipeline_layout.c:1139–1230` zeigt zwei unterschiedliche Buffer-
Zugriffe: indirekte Adresse/Range-Deskriptoren und direkte Surface-State-Daten.
`anv_private.h:3439` definiert für den indirekten gebundenen globalen Zugriff
eine 16-Byte-Struktur mit 64-Bit-Adresse, 32-Bit-Range und 32-Bit-Nulloffset.
Robuste UBO-/SSBO-Adressformate verwenden
`nir_address_format_64bit_bounded_global`. Dieser explizite A64-Pfad ist ein
möglicher enger erster Port; ob der übersetzte Shader diesen Pfad nutzt,
entscheidet das echte Lowering. Texture-/Sampler-/BTI-Zugriffe dürfen dabei
nicht stillschweigend stehen bleiben.
[Descriptor-Lowering](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/vulkan/anv_nir_apply_pipeline_layout.c)

Die Compiler-Ausgabe muss mindestens folgende getrennte Felder mitführen:

| Ausgabe | Bedeutung für den nativen Dispatcher |
| --- | --- |
| `base.program_size`, `const_data_offset/size`, `num_relocs/relocs` | Tatsächliche Code-/Konstantenlängen und typisierte BRW-Relokationen. Copy-out vor Freigabe des Compiler-Kontexts; unbekannte Relokationen ablehnen. |
| `local_size[3]`, `prog_mask`, `prog_offset[3]` | Tatsächlich kompilierte SIMD-Varianten und deren ISA-Einstiege. Weder SIMD32 noch ein bestimmter ISA-Offset ist ohne Compileroutput garantiert. |
| `base.grf_used`, `dispatch_grf_start_reg`, `total_scratch`, `total_shared` | Register-/Scratch-/SLM-Anforderungen; eine erste enge Probe kann Scratch/Shared/Barriers ablehnen, darf sie nicht ignorieren. |
| `generate_local_id`, `walk_order`, `uses_barrier`, `uses_sampler` | Local-ID-Generierung und Zugriffsfunktionen müssen zum Walker passen. |
| `push.cross_thread` / `push.per_thread`, `base.push_sizes[4]` | Compiler-Push-Längen mit ihren Einheiten. Push-`regs` messen weiterhin 32-Byte-Blöcke, obwohl Xe2-GRFs 64 Byte groß sind. |
| `bind_map.push_ranges[4]`, `inline_dwords_count`, `inline_dwords[8]` | Welche Push-/Driverwerte der Runtime wirklich übertragen muss; diese Information gehört neben die ISA. |

`brw_compile_cs.cpp:29–54` legt bei `verx10 >= 125` den ersten Push-Block
vollständig in Cross-Thread-Daten; Per-Thread-Push ist dort 0. Das bedeutet
nicht, dass keine Local-ID-Threadpayload existiert.
`brw_thread_payload.cpp:308–344` bildet R0, optionale lokale Koordinaten und
Inline-Parameter als Registerpayload ab. `brw_nir_lower_cs_intrinsics.c:299–363`
kann für feste Gruppen mit geeigneten Dimensionen Hardware-Local-IDs wählen;
die Emit-Masken sind X=1, XY=3 oder XYZ=7. Für 64×1×1 ist X-generiert als
Compilerergebnis zu prüfen, nicht vorab als beobachtet zu melden.
[Compute-Compiler](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/compiler/brw/brw_compile_cs.cpp),
[Local-ID-Lowering](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/compiler/brw/brw_nir_lower_cs_intrinsics.c),
[Threadpayload](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/compiler/brw/brw_thread_payload.cpp)

`anv_nir_compute_push_layout.c:542–595` macht den entscheidenden Inline-
Unterschied sichtbar. Bei weniger als acht tatsächlich benötigten Push-DWORDs
überträgt ANV ausgewählte DWORD-Indizes direkt. Sonst können die ersten zwei
Inline-DWORDs die 64-Bit-Adresse des Push-Buffers enthalten; weitere Einträge
tragen ausgewählte Driverdaten. `fill_inline_param(s)` in
`genX_cmd_compute.c:434–463` wertet die Selektoren aus. Die ersten 32 Byte eines
OpenCL-Cross-Thread-Buffers dorthin zu kopieren wäre ohne diese Map falsch.
[Push-Lowering](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/vulkan/anv_nir_compute_push_layout.c),
[Runtime-Inline-Füllung](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/vulkan/genX_cmd_compute.c)

Der zukünftige Compileradapter sollte deshalb eine eigene versionierte
Festbreiten-Darstellung für ISA, ProgData-Auszug, Descriptor-/Pushmap und
Relokationen ausgeben. Rohes `memcpy` von Mesa-Strukturen ist keine dauerhafte
User-/Kernel-ABI: Zeiger, Padding und Versionsänderungen gehören nicht in ein
unabhängiges Treiberformat. SHA256 des ursprünglichen MSL/AIR/SPIR-V, des
Compilerstands und jedes erzeugten Code-/Metadatenblobs gehören in dessen
Provenienz. Mesas 64-Bit-`source_hash` ersetzt diese vollständigen Hashes nicht.

## Xe2-Walker und ISA-Speicher: belegte Runtime-Pflichten

Der am Pin gelesene `src/intel/genxml/xe2.xml` definiert `COMPUTE_WALKER` mit
**40 DWORDs / 160 Byte**, einen Body mit 39 DWORDs und einen InterfaceDescriptor
mit acht DWORDs. Body-DWORD18 enthält den Descriptor; InlineData beginnt am
Body-DWORD31, also **Gesamtpacket-DWORD32**, und umfasst acht DWORDs.
`Kernel Start Pointer` ist ein Offset mit Bits 31:6. ANV verwendet
`shader->kernel.offset`, während `STATE_BASE_ADDRESS.InstructionBaseAddress`
auf den Shader-Heap zeigt. Das ist keine CPU-/System-Physical-Adresse.
`anv_shader.c:793` allokiert die ISA mit 64-Byte-Alignment.
[Xe2-Paketdefinition](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/genxml/xe2.xml),
[CS-Descriptor](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/vulkan/genX_shader.c),
[State-Base-Programmierung](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/vulkan/genX_cmd_buffer.c)

`brw_cs_get_dispatch_info` berechnet aus der tatsächlich ausgewählten SIMD-
Variante die Threadanzahl und rechte Ausführungsmaske. `emit_cs_shader` setzt
GenerateLocalID/EmitLocal/WalkOrder, lokale Maxima, Threadanzahl, SLM und
Barriers anhand dieser Compileroutputs. Registerblock-Encoding für
`GFX_VER >= 30` gehört nicht ungeprüft zu Lunar Lake `GFX_VER == 20`.
Für Paketbytes sind die Generatoren und XML-Daten desselben Pins zu verwenden;
ein älterer Gen12-Descriptor ist kein passender Ersatz.

Zwei zusätzliche Speichergates sind bereits in ANV konkret implementiert:

1. `anv_shader_heap.c:139–156` berücksichtigt **3584 Byte Instruction-
   Overfetch** hinter dem Kernel, begrenzt durch die programmierte Heapgrenze.
   Nur `program_size` zu mappen darf nicht als vollständiger ISA-Speicherplan
   angenommen werden.
2. `anv_shader_set_relocs` / `anv_shader_reloc_begin` in `anv_shader.c:547–703`
   setzen Shader-Heap-/Descriptor-Highbits, Constant-Data-Adressen und weitere
   tatsächlich angeforderte BRW-Relokationen über `brw_write_shader_relocs`.
   Mehrere ANV-VA-Heaps haben am Pin ein 4-GiB-Alignment. Ein XNU-Port muss
   diese VA-Annahmen erfüllen oder das zugehörige Lowering ändern; Nullwerte
   oder OpenCL-Zebin-Relokationsregeln sind kein gültiger Standardersatz.

Quellen: [Shader-Heap](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/vulkan/anv_shader_heap.c),
[ISA-/Relokationsanlage](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/vulkan/anv_shader.c).

## Kleinster überprüfbarer CPU-Bauweg und nächster Port

Der vorhandene CPU-Parser ist `spirv2nir`. Sein Meson-Target steht in
`src/compiler/spirv/meson.build`; Vulkan ist seine Standardumgebung und `--stage
cs` wählt Compute. Er kann NIR ausgeben, **keine Xe2-ISA**. Folgendes ist ein
aus dem Pin abgeleitetes, hier **noch nicht ausgeführtes** Startrezept in einer
vollständigen privaten Mesa-Checkout-Kopie:

```sh
meson setup build-cpu \
  -Dplatforms=[] -Dgallium-drivers=[] -Dvulkan-drivers=[] \
  -Dvulkan-layers=[] -Dopengl=false -Dgles1=disabled -Dgles2=disabled \
  -Degl=disabled -Dgbm=disabled -Dglx=disabled -Dllvm=disabled \
  -Dtools=nir -Dbuild-tests=false
meson compile -C build-cpu spirv2nir
build-cpu/src/compiler/spirv/spirv2nir --stage cs --entry main shader.spv
```

Der Entry-Name muss aus dem tatsächlichen SPIR-V/Reflection stammen.
`spirv2nir` benutzt allgemeine NIR-Options; seine Parserabnahme qualifiziert
noch nicht das Intel-Lowering. Für einen nachfolgenden eigenen Adapter sind
`compiler->nir_options[MESA_SHADER_COMPUTE]` und tatsächliche Capabilities/
Addressformate erforderlich. `spirv_to_nir_options.ignore_replacement = true`
verhindert unbemerkten Shaderaustausch über `MESA_SPIRV_READ_PATH`;
`skip_os_break_in_debug_build` erlaubt kontrollierte Parserfehler.
[spirv2nir-CLI](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/compiler/spirv/spirv2nir.c)

Die kleinsten folgenden Arbeitsschritte sind konkret:

1. Vollständigen Checkout **dieses** Pins mit Unterabhängigkeiten beschaffen,
   Hash-/Lizenzinventar und private Buildumgebung erzeugen. Zuerst nur den
   echten `spirv2nir`-Build und die tatsächliche Shader-Reflection qualifizieren.
2. In einem separat reviewten Mesa-Buildpatch einen Compiler-only-Schalter
   ergänzen. `with_any_intel` wird upstream durch Intel-Gallium/Vulkan/Tools
   gesetzt. Für BRW benötigt werden `mda`, `genxml`, `dev`, `isl`,
   `compiler` und MesaUtil/NIR/VTN. `idep_intel_compiler_brw` aus
   `src/intel/compiler/brw/meson.build:151` linkt `libintel_compiler_brw` und
   `libisl` plus NIR/Util/VTN. Ein eigenes enges CLI-Target kann daran linken.
3. Ausschließlich den eigenen Compute-Fall mit drei SSBOs, fester Gruppe,
   vollständigen Buffer-Extents und expliziter Descriptor-/Push-Lowering-
   Konfiguration unterstützen. Weitere Capabilities, Shared Memory,
   Sampler/Textures, Barriers, Atomics und ungeklärte Relokationen zunächst
   mit begründetem Fehler ablehnen. Kein leerer ANV-Geräte-Dummy.
4. Wirklich `brw_compile` aufrufen, Null-/Fehlerausgabe prüfen und ISA samt
   ProgData/Bindmap/Relocs atomar in den eigenen versionierten Vertrag kopieren.
   Vor Freigabe des Kontextes Längen, SIMD-/Entry-Masken, Constant-/Reloc-
   Grenzen und fehlende Resource-/Systemvalue-Lowerings prüfen. Die echte
   Intel-ISA-Validierung/Disassemblierung desselben Compilerpins anschließen.
5. Erst nach dem CPU-Compilebeleg native PPGTT-/Descriptor-/Push-/ISA-Heaps,
   Cache-/TLB-/CFE-Zustand, laufenden GuC und Completion mit unserem
   Dispatcher verbinden. Eine GPU-Probe braucht tatsächlichen Fenceabschluss
   und den Vergleich aller 65536 Outputwerte. CPU-Referenzrechnen ist dabei
   ausschließlich die Vergleichsgrundlage, kein Ersatz für GPU-Ausführung.

Am Pin fordert die Wurzel-Meson-Konfiguration Meson ≥1.4, C11/C++17, Python
≥3.10 mit Mako ≥0.8, Packaging/Distutils und PyYAML; Ninja/Pkg-Config und
generierte NIR-/SPIR-V-/Device-/Workaround-/Genxml-Daten werden für diesen
Buildgraph benötigt. `-Dtools=intel` aktiviert zusätzlich Expat/Flex/Bison und
weitere Intel-Tools einschließlich Executor. Es ist kein minimaler Compiler-
only-Schalter. Die Library-Abhängigkeiten ergeben sich aus den
[BRW-Meson-Dateien](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/compiler/brw/meson.build)
und [Intel-Unterverzeichnissen](https://github.com/mirror/mesa/blob/e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae/src/intel/meson.build).

Für Hosts ohne DRM stellt `src/util/libdrm.h` nicht erfolgreiche Stubantworten
bereit; Meson kann `dep_libdrm = null_dep` setzen. Das unterstützt einen
offline CPU-Buildansatz, **keine GPU-Laufzeit**.
Der vollständige ANV-Treiber verlangt am Pin zusätzlich `dl_iterate_phdr`
(`meson.build:1725–1728`) und Linux-/DRM-KMD-Dienste. Diese Runtimezweige
müssen aus dem ersten Darwin-Compilerbuild herausgehalten werden. Mesas
[allgemeine Meson-Unterstützung für macOS](https://docs.mesa3d.org/meson.html)
beweist keinen bestehenden ANV-Darwin-Treiber.

Ein ausführbarer neuer Compiler-Harness wurde in dieser Etappe nicht
veröffentlicht: vorhanden waren selektive Primärdateien, keine vollständige
generierte Mesa-Buildbasis und keine qualifizierte Meson/Ninja-Umgebung.
`src/intel/executor/executor_main.c` ist ebenfalls kein Ersatz: Es öffnet DRM-
Rendergeräte und benutzt Xe-VM-/Queue-/Exec-/Syncobj-Dienste. Diese gehören in
einen späteren XNU-KMD-Port und werden nicht als CPU-Compilerprobe ausgeführt.

## Native Grenzen und Lizenz

Diese Route kann Shader-Maschinencode liefern. Sie registriert kein
`MTLDevice`, implementiert keine Metal-System-Userclient-ABI, IOSurface-/IOAccel-
Anbindung, Displayengine oder WindowServer-Beschleunigung. Nachweisbarer
GuC-/PPGTT-Compute auf Arc 130V und native macOS-Metal-Geräteintegration bleiben
getrennte nötige Abnahmen. Ein Intel-macOS-CI-Compilerlauf würde CPU-Toolchain-
Portabilität prüfen; er wäre kein Zielhardwarebeweis.

Dieser Anschlussentwurf und spätere eigene Harnesscode stehen unter
GPL-3.0-only. Mesa bleibt eine externe Compilerquelle mit ihren eigenen
[Dateilizenzen](https://docs.mesa3d.org/license.html); viele der untersuchten
Intel-/NIR-Bausteine sind MIT. Tatsächlich übernommene Dateien und generierte
Abhängigkeiten benötigen ihr vollständiges Lizenz-/Copyrightinventar.
Der AIR-Converter behält separat seine LGPL-Bedingungen. Hier wurden keine
externen Compilerquellen oder proprietären Apple-Binaries ins Repository
übernommen.

## Ausgewählte SHA256-Belege des untersuchten Pins

| Originaldatei | SHA256 |
| --- | --- |
| `src/compiler/spirv/nir_spirv.h` | `313508c689c0a34c1464d5b964b7d6223a8ac9c3ee61f3a5635ff8e288edfe93` |
| `src/intel/compiler/brw/brw_compiler.h` | `0b7a31ce01ad67a6797b238fae5f731ffb0e3ddefa0f276a11d2428f2dee0c3f` |
| `src/intel/compiler/brw/brw_compile_cs.cpp` | `f6cf03792c30d877e4c73709af15fdeb5ccdbaa0c8e8689b4438c4b7b375f776` |
| `src/intel/vulkan/anv_shader_compile.c` | `be43ff76d952d6caee8123db8317290380ec19fc8a9ab28ffa2c567c8c65a8b4` |
| `src/intel/vulkan/anv_nir_compute_push_layout.c` | `768d0dfb767d481aaf3e80871025f8745e349acdeb1f0dcb0a4b3b5d557489b9` |
| `src/intel/genxml/xe2.xml` | `c5de8470028ea9ff9c2aed14e1478968a34d99dba70729d9b140b9426e84949d` |
| `src/intel/vulkan/anv_shader_heap.c` | `9a1fa58be5cf11055078b3307e14431cf03a100510e600a38146db077f2c2e1f` |
| `src/intel/dev/intel_device_info.c` | `014a3a0e388a1ca30732a09a73364a9cca8a5f54c0a0a52b2aa34a45cf525e62` |
