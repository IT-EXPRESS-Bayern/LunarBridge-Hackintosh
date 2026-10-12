<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Arc 130V: Linux-Treiberschnittstelle gegen macOS-Tahoe-Kontrakt

Stand: 12. Oktober 2026. Dieses Dokument vergleicht die **Schnittstellen** des
Linux-Arc-130V-Treibers mit dem Kernelkontrakt von macOS 26 (Tahoe) und
beschreibt, was am Arc-Pfad dieses Projekts daraufhin geändert wurde. Alle
Quellen sind hash-gepinnt in [ARC_INTERFACE_SOURCES.json](ARC_INTERFACE_SOURCES.json);
der maschinell erzeugte Vergleich steht in
[ARC_INTERFACE_DIFF.json](ARC_INTERFACE_DIFF.json).

Es wurde **kein Apple- oder Intel-Treibercode übernommen**, keine Apple-Binärdatei
gelesen oder weitergegeben und kein Gerät angesprochen. Eine akzeptierte
Anfrage ist eine geprüfte Aufrufform, **keine Submission und keine
GPU-Ausführung**.

## Was tatsächlich abgerufen wurde

| Seite | Pin | Rolle |
| --- | --- | --- |
| Linux-Arc-130V-Treiber | `torvalds/linux` `e5f0a698b34ed76002dc5cff3804a61c80233a7a` (v6.17) | `include/uapi/drm/xe_drm.h` mit 12 ioctls, dazu `drm.h`, die Treiber-ioctl-Tabelle und die bedienenden Quellen für Exec, Queue, Query und Sync |
| macOS-26-Kernelkontrakt | `apple-oss-distributions/xnu` `xnu-12377.121.6` | `IOUserClient.h` (Externalmethod-Vertrag), `IOMemoryDescriptor.h`, `IODMACommand.h` |
| macOS-Grafikfamilie | `apple-oss-distributions/IOGraphics` `IOGraphics-600` | `IOAccelerator.h`, `IOAccelClientConnect.h`, `IOAccelSurfaceConnect.h`, `IOAccelTypes.h`, `IOFramebuffer.h` |
| macOS-PCI | `apple-oss-distributions/IOPCIFamily` `IOPCIFamily-726.100.6` | `IOPCIDevice.h`, bereits von der Diagnose-Kext benutzt |
| Apple-Intel-Treiberinventar | `acidanthera/WhateverGreen` `093e76fec5655001f38b367dd22459c756ee89fa` | Öffentliche Liste der von Apple ausgelieferten Intel-Grafik-Kexte |

Die Apple-Tags sind die neuesten veröffentlichten Stände zum Erhebungsdatum.
Die xnu-12377-Linie ist die macOS-26-Kernellinie, gegen die dieses Projekt
ohnehin baut (`macos-26-intel`-CI, `xnu-12377.1.9`-Verweis im Kextbuild). Der
Marketingname lässt sich aus diesen Quellen **nicht** belegen.

## Der beste Grafiktreiber von macOS Tahoe für Intel ist Gen11, nicht Xe2

Das öffentliche Kextinventar enthält **20** Apple-Intel-Grafikbundles. Die
jüngste Familie ist Ice Lake (Gen11):

* `com.apple.driver.AppleIntelICLGraphics` – Accelerator-/Metal-Treiber
* `com.apple.driver.AppleIntelICLLPGraphicsFramebuffer` und
  `…ICLHPGraphicsFramebuffer` – Displayseite

Für Gen12, Xe oder Xe2 existiert **kein** Apple-Treiber. Die öffentliche
Plattformtabelle stellt das ausdrücklich fest: Rocket Lake, Alder Lake, Raptor
Lake und Arrow Lake setzen gemeinsam `gPlatformGraphicsSupported = false`;
Lunar Lake erscheint dort nicht einmal. Es gibt damit keinen
„Arc-Treiber von macOS", der modifiziert werden könnte: Die macOS-Seite dieses
Vergleichs ist der **Kernel- und Userclient-Kontrakt**, nicht ein Intel-Treiber.
Der Arc-Treiber, der tatsächlich existiert und Schnittstellen beschreibt, ist
der Linux-Xe-Treiber; modifiziert wurde deshalb der **Arc-Pfad dieses Projekts**
so, dass er die macOS-Form erfüllt statt die Linux-Form zu kopieren.

Zusätzlich belegt die öffentliche Apple-Quelle, wie schmal die veröffentlichte
Beschleunigerschnittstelle ist: `IOAccelerator` exportiert ausschließlich
`createAccelID`/`retainAccelID`/`releaseAccelID`, es gibt genau einen
öffentlichen Clienttyp (`kIOAccelSurfaceClientType`) und **18**
Surface-Methoden für Locking, Shape und Flush. Eine veröffentlichte
Kommando-Submission-, Page-Table- oder Queue-Schnittstelle existiert nicht. Was
ein Metal-Treiber zusätzlich erfüllen muss, ist privat und bleibt eine offene
Anforderung dieses Projekts.

## Die beiden Schnittstellen im direkten Vergleich

| Eigenschaft | Linux Xe (Arc 130V) | macOS 26 Userclient |
| --- | --- | --- |
| Einstieg | Render-Node `/dev/dri/renderD*`, 12 ioctls ab `DRM_COMMAND_BASE` 0x40 | `IOServiceOpen` auf einen `IOUserClient`, Selector-Index in eine Dispatchtabelle |
| Berechtigung | Je Eintrag `DRM_RENDER_ALLOW` (Dateirechte am Render-Node) | `IOExternalMethodDispatch2022` prüft `checkEntitlement`, dazu Clienttyp und `IOUserClient`-Admissionslogik |
| Argumentform | Pro ioctl eine eigene Struktur mit `__u64 extensions` und Userpointern | `IOExternalMethodArguments`: bereits kopierte Skalare und **eine** Struktur fester Länge |
| Längenprüfung | `_IOC_SIZE` plus treiberseitige Feldprüfung | `checkScalarInputCount`, `checkStructureInputSize`, `checkScalarOutputCount`, `checkStructureOutputSize`; `kIOUCVariableStructureSize` = `0xFFFFFFFF` deaktiviert die Prüfung |
| Skalargrenze | Keine (Struktur trägt alles) | `kIOExternalMethodScalarInputCountMax` = `kIOExternalMethodScalarOutputCountMax` = **16** |
| Erweiterbarkeit | Verkettete `drm_xe_user_extension` über `next_extension` | Keine Verkettung; neue Felder brauchen eine neue, geprüfte Strukturversion |
| Objekte | GEM-Handles je offener Datei, `drm_syncobj`, Fake-`mmap`-Offsets | `IOMemoryDescriptor`/`IODMACommand`, `clientMemoryForType`, kein Syncobj-Namensraum |
| Asynchronität | Blockierende ioctls, Timeout `-1` erlaubt | `asyncReference`/`asyncWakePort` als Rückkanal; ein Kernelwait muss begrenzt sein |

## Abbildung der 12 Arc-ioctls

`lb_arc_interface.cpp` führt eine Tabelle mit 11 Operationen.
`tools/compare_interfaces.py` prüft jede Zeile gegen den gepinnten Header:
ioctl-Nummer, Richtung und die tatsächlich vorhandenen Linux-Konstrukte.

| Operation | Linux-ioctl (Nr.) | Struktur | Konstrukte im Header | macOS-Antwort |
| --- | --- | --- | --- | --- |
| `LBArcOpDeviceQuery` | `DEVICE_QUERY` (0x40, IOWR) | `drm_xe_device_query` | Extensions, Userpointer | Feste Satzanzahl statt Rückschreiben über `data` |
| `LBArcOpResourceCreate` | `GEM_CREATE` (0x41, IOWR) | `drm_xe_gem_create` | Extensions, Handle | Besitzergebundenes Token, PAT 2 explizit geprüft |
| `LBArcOpResourceMap` | `GEM_MMAP_OFFSET` (0x42, IOWR) | `drm_xe_gem_mmap_offset` | Extensions, Handle, Mmap-Offset | `clientMemoryForType` Typ 0; kein Fake-Offset |
| `LBArcOpVmCreate` | `VM_CREATE` (0x43, IOWR) | `drm_xe_vm_create` | Extensions, Handle | Expliziter VA-Bereich, Scratch-/Fault-Mode abgelehnt |
| `LBArcOpVmDestroy` | `VM_DESTROY` (0x44, IOW) | `drm_xe_vm_destroy` | Handle (**keine** Extensions) | Token des Besitzers |
| `LBArcOpVmBind` | `VM_BIND` (0x45, IOW) | `drm_xe_vm_bind` | Extensions, Userpointer, Vektor, Sync-Array, Handle | Begrenztes Inline-Segmentfeld, nur User-Fences, PAT 2, Fensterprüfung |
| `LBArcOpQueueCreate` | `EXEC_QUEUE_CREATE` (0x46, IOWR) | `drm_xe_exec_queue_create` | Extensions, Userpointer, Handle | Feste GuC-Klasse/Maske statt `instances`-Pointer |
| `LBArcOpQueueDestroy` | `EXEC_QUEUE_DESTROY` (0x47, IOW) | `drm_xe_exec_queue_destroy` | Handle (**keine** Extensions) | Token des Besitzers |
| `LBArcOpQueueProperty` | `EXEC_QUEUE_GET_PROPERTY` (0x48, IOWR) | `drm_xe_exec_queue_get_property` | Extensions, Handle | Eine begrenzte Auskunft (Phase) |
| `LBArcOpExec` | `EXEC` (0x49, IOW) | `drm_xe_exec` | Extensions, Userpointer, Sync-Array, Handle | Inline-Fences, 64-Byte-ausgerichtete Batchadresse, Signalpflicht |
| `LBArcOpFenceWait` | `WAIT_USER_FENCE` (0x4a, IOWR) | `drm_xe_wait_user_fence` | Extensions, Handle, Fenceadresse, unbegrenzter Wait | Begrenzter Timeout, genau ein User-Fence |

`DRM_XE_OBSERVATION` (0x4b) bleibt **absichtlich unabgebildet**: Das ist der
OA-/Perf-Pfad mit eigener Streamsemantik; er gehört nicht in die erste
Arc-Bringup-Schnittstelle.

Dieser Abgleich hat bereits einen echten Fehler in der eigenen Tabelle
gefunden: `drm_xe_vm_destroy` und `drm_xe_exec_queue_destroy` tragen **keine**
Extension-Kette. Die vorherige Handannahme war falsch und wurde korrigiert, statt
die Behauptung stehen zu lassen.

## Die kodierten Unterschiede

Jede Operation führt die Bits mit, die ihre Portierung beschreiben
(`LBArcDifference`). Fünf davon sind an ein einzelnes Linux-Konstrukt gebunden
und werden in **beide** Richtungen geprüft – ein behauptetes Bit ohne Konstrukt
überzeichnet die Portierung, ein Konstrukt ohne Bit heißt, dass die
macOS-Schnittstelle etwas stillschweigend geerbt hat:

| Bit | Bedeutung | Gebunden an |
| --- | --- | --- |
| `NoUserPointer` | Es werden nur kopierte Daten gelesen | `__u64`-Userpointer (`data`, `syncs`, `instances`, `vector_of_binds`, `userptr`) |
| `FlattenedVector` | Begrenztes Inline-Array statt Vektor | `vector_of_binds` |
| `NoSyncObject` | Kein `drm_syncobj`-Namensraum | `drm_xe_sync`-Array |
| `NoMmapOffset` | `clientMemoryForType` statt Fake-Offset | `drm_xe_gem_mmap_offset.offset` |
| `BoundedTimeout` | Kernelwait ist begrenzt | `__s64 timeout` (−1 = ewig) |

Die übrigen Bits folgen aus der Aufrufform oder der Projektpolitik und gelten
unabhängig vom einzelnen Feld: `FixedStructSize`, `NoExtensionChain`,
`OwnerBoundToken`, `NoRenderNode`, `EntitlementGate` (alle Operationen) sowie
`ExplicitPat` und `AsyncReference` (operationsabhängig).

## Was am Arc-Pfad geändert wurde

1. **Neues Modul `gpu-prototype/lb_arc_interface.{h,cpp}`.** Es veröffentlicht
   die Operationstabelle (576 Byte), prüft die Aufrufform gegen den
   XNU-Externalmethod-Vertrag und validiert eine flache Anfrage (480 Byte) zu
   einem adressfreien Urteil (80 Byte). Felder außerhalb der jeweiligen
   Operation müssen null sein, sonst könnte eine gemeinsame Struktur Zustand an
   den Prüfungen vorbeischmuggeln. Akzeptierte Bindungen melden Seitenzahl,
   GGTT-Eintragszahl und erwarteten PAT-Wert; `lb_ggtt_build_plan` bleibt die
   Instanz, die alles erneut prüft.
2. **Linux-Konstrukte werden benannt abgelehnt**, nicht stillschweigend
   ignoriert: Userptr-Bind, `UNMAP_ALL`, `PREFETCH`, Syncobj- und
   Timeline-Syncobj-Typen, zweiter Speichertyp, unbegrenzter Timeout,
   variable Strukturlänge, Skalare, Async-Referenzen.
3. **Besitz kommt vom Userclient.** `owner` muss in der Anfrage 0 sein; die
   Kext stempelt ihre Clientkennung ein. Linux leitet Besitz aus dem offenen
   Render-Node ab – ein vom Client gesetztes Feld wäre genau die Stelle, an der
   sich ein anderer Client fälschen ließe.
4. **ABI 1 additiv erweitert**: Selector 7 liefert die Tabelle, Selector 8
   prüft eine Anfrage, Selector 9 berichtet den
   [portierten Xe2-Plan](ARC_PORT_FROM_LINUX.md). Selector 0 bis 6 bleiben unverändert, Selector 4
   (`LBSubmit`) bleibt ausdrücklich `kIOReturnUnsupported`. Trace-Ereignisse
   60 bis 62 halten Abfrage, Annahme und Ablehnung fest.
5. **Werkzeuge und Builds**: `tools/compare_interfaces.py` erzeugt den
   Vergleich und verifiziert die kompilierte Tabelle gegen die Quellen;
   `tools/lb_wire.py` dekodiert Tabelle und Urteil in Userspace;
   `tools/collect_macos.py` liest die Tabelle eines geladenen Treibers mit;
   `build_gpu.py`, `tools/build_native.py` und die CI bauen und prüfen das
   Modul mit.

## Verifikation und ihre Grenzen

| Nachweis | Umfang |
| --- | --- |
| `tests/test_arc_interface.py` | 46 Tests gegen die **kompilierte** Bibliothek: Wire-Größen, Tabelleninhalte, Aufrufformen, Annahme-/Ablehnungsmatrix je Operation, Besitzbindung, Aliasing, Userspace-Dekodierung |
| `tests/test_interface_diff.py` | 13 Tests gegen die **gepinnten Quellen**: 12 ioctls, Nummern, Richtungen, Konstrukte, Apple-Kontraktwerte, Inventarschluss, Abgleich der kompilierten Tabelle |
| `tools/compare_interfaces.py --library …` | Tabelle gegen Quellen: keine Abweichung |
| `.github/workflows/interface-diff.yml` | Linux-CI: Quellen holen, Modul bauen, beide Testsuiten, erneut erzeugter Vergleich muss dem eingecheckten entsprechen |
| `.github/workflows/native-build.yml` | Intel-macOS-CI: Modul in der Host-Bibliothek und im Kextbuild |

Nicht nachgewiesen bleiben: geladene Kext, Geräteanbindung, GGTT-Hardware­besitz,
programmiertes PAT 2, laufende GuC, echte Submission, Fenceabschluss,
Metal-Gerät und Display. `gpuExecutionImplemented` und
`metalProviderImplemented` sind in der Tabelle 0, und jedes Urteil meldet
`hardwareBound = gpuExecuted = metalVerified = 0`.

## Nächste Schritte

1. Selector 7/8 auf dem Zielgerät gegen eine geladene Kext lesen und das
   Ergebnis mit `tools/collect_macos.py` erfassen.
2. `LBArcOpVmBind` an den vorhandenen GGTT-Planer und den Geräte-DMA-Mapper
   binden, dann die fehlenden Qualifikationen einzeln abbauen.
3. Für `LBArcOpQueueCreate`/`LBArcOpExec` die GuC-Registrierung mit echtem
   LRC-/ADS-Speicher und einer laufenden CT-Queue verbinden.
4. Erst danach die private Metal-Anbieterseite angehen; der öffentliche
   IOAccel-Surface-Vertrag deckt sie nicht ab.
