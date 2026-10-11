# Nativer Arc-130V-Anschluss und eigener Xe2-Shader

Der erste Versuch wird ausdrücklich **ohne Firmware-UEFI-Secure-Boot** vorbereitet. Die Profilauswahl steht im separaten [OpenCore-Modul](https://github.com/IT-EXPRESS-Bayern/LunarBridge-OpenCore/blob/main/docs/TEST_PROFILES.md). Sie ersetzt weder die native Kext-Zulassung noch den fehlenden Grafikstapel. Die externe SSD und Windows-Startauswahl bleiben das Ziel.

| Stufe | Implementierung und bestätigte Prüfung |
| --- | --- |
| Eigener Xe2-Rechenkern | Unter Windows auf der tatsächlichen Arc 130V kompiliert und einmal ausgeführt; 64/64 Ergebniswerte korrekt, eigener ISA-/SPIR-V-Code und Relokationen extrahiert |
| Native Register-Anbindung | IOKit-Adapter für genau `8086:64A0`, Subsystem `144D:C1DD`; als echtes x86_64-macOS-Objekt kompiliert, noch keine native Geräteprobe |
| Main-GT-Aufwecken | Produktions-C++ und nativer Adapter implementiert; 29 Callback- und 43 Adaptertests bestanden, insgesamt 52 Adapter-Szenarien; Testdaten ausdrücklich synthetisch |
| Native GPU-Speicherbindung, Firmware und Ausführung | Noch offen; vorhandenes CPU-PTE-Staging ist keine Hardwarebindung |
| Metal, Display und Standby | Noch offen; weder der Windows-Shader noch eine Wake-ACK beweist diese Funktionen |

Der eigene Shaderbaustein samt nachgewiesener Herkunft ist in [gpu-toolchain](../gpu-toolchain/README.md) enthalten. Die tatsächliche historische Probe bleibt an ihren Quellhash gebunden; die danach für öffentliche Nutzung überarbeitete CLI wurde nicht erneut auf Hardware ausgeführt. 37 zusätzliche Offline-Tests prüfen Auswahl, Ergebnisbytes und ELF/Zebin-Grenzen.

## Opt-in-Aufwecken mit kontrolliertem Abbau

`LBEnableForceWakeProbe=true` aktiviert beim Service-Start genau eine Main-GT-Get/Put-Transaktion. Standardmäßig ist die Eigenschaft `false`. Der Adapter überprüft exakte PCI-/Subsystem-/Klassenwerte, PM-Capability, Memory-Decode, Provider-On, PCI-D0, Provider-Open und die vollständige BAR-Abdeckung bis `0x380D90`. Vor jedem Registerzugriff wird der aktuelle Zustand erneut geprüft; er wird nicht automatisch verändert.

Die tatsächlichen GMD-Werte müssen Grafik-IP `2004` und Media-IP `2000` ergeben. Ein **tatsächlich gelesener** Media-GMD-Wert null gilt als fused-off. Ein ausgefallener Media-Read wird niemals als null ausgegeben. Unerwartete IPs und All-ones-MMIO werden abgewiesen.

Der Main-GT-Request verwendet Control `0xA188` mit maskierten Werten `0x10001` für Get und `0x10000` für Put. Die ACK stammt von `0xDFC`, Kernelrequestor-Bit 0. Vorhandenes Control- oder ACK-Bit 0 wird als fremde Anforderung behandelt; es wird nicht übernommen oder freigegeben. Andere Requestorbits bleiben erhalten. Get und Put haben jeweils eine 50-ms-ACK-Frist, zusätzlich eine feste Pollgrenze; die Frist wird vor und nach jedem Read kontrolliert. Ein verspäteter ACK bestätigt keinen Erfolg. Thread-Descheduling kann die tatsächliche Laufzeit verlängern; es erzeugt keinen falschen rechtzeitigen ACK.

**Lunar-Lake-Workaround WA15015404425:** vor jedem MMIO-Read erfolgen vier Root-BAR-Dummywrites von null an `0x130030`. Das betrifft auch die GMD-Probe. Ein als reines, schreibfreies MMIO-Lesen beschriebener Pfad wäre hier unvollständig; das bisherige Read-only-BAR-Mapping wurde ersetzt. Ohne beide Opt-ins erfolgen weiterhin keine MMIO-Zugriffe. Es gibt keine PCI-Command-/D0-, Busmaster-, GGTT-, PAT-, WOPCM-, Firmware- oder Engine-Programmierung.

Ein fehlgeschlagener Get kann bereits einen Request ausgelöst haben. Daher wird auch in diesem Fall ein Put versucht. Bei fehlgeschlagenem Put bleiben eigene Lease, BAR-Mapping, Provider-Open und PCI-/Servicereferenzen gehalten. Ein späterer administrativer Verbindungsaufbau oder Service-Stop versucht den begrenzten Abbau erneut. Erst ein bestätigter ACK-Clear erlaubt Map-Release, Close und Referenzfreigabe. Fehlende native Power-Transition-Synchronisierung bleibt ein Geräteprüfpunkt; PCI-Rechecks schließen einen Zustandswechsel unmittelbar danach nicht aus.

## Status, Installer und tatsächlicher Build

ABI 1 wird um **Selector 6 / 80 Byte** erweitert; bestehende Selector-Layouts bleiben erhalten. Exportiert werden Status, Get/Put-Abbau, Read-/Request-/Flushzählung, ACK, beobachtete IP-Versionen und gehaltene Ressourcen. Adressen werden nicht ausgegeben. `gpuExecuted=0` und `metalVerified=0` bleiben Pflicht. Ereignisse 50–53 unterscheiden Handshake, Fehler und Cleanup. Der Sammler liest die neue Ausgabe nur bei der aktivierten Driver-Eigenschaft und erhält Snapshot/Traces auch bei Fehlern.

Auf einem Mac-Buildhost:

```sh
python3 tools/build_native.py --sdk /path/to/MacKernelSDK --out build/native-probe --forcewake-probe --debug-log
```

Die native CI baut getrennte Diagnose- und Forcewake-Probe-Artefakte. Beide sind unsigniert; die Probevariante enthält den Opt-in bereits im `Info.plist`. Der Installerplan zeigt `forcewake_probe_enabled` und `lnl_read_flush_writes_enabled` an. Er aktiviert die Kext nur mit seinem vorhandenen ausdrücklichen `--enable`; eine kopierte Kext ist kein Ladetest. Das Abschalten von Firmware Secure Boot erlaubt keine automatische SIP-/AMFI-Änderung und bestätigt keine native Kext-Zulassung.

Nach einem tatsächlichen nativen Kext-Start:

```sh
sudo python3 tools/collect_macos.py --out ./arc130v-native-bericht
```

Ein erfolgreicher Handshake bestätigt ausschließlich Aufwecken und Freigabe des Main-GT. Als nächste echte GPU-Stufen fehlen ein serviceeigener GGTT-Allocator mit Scratch-/PAT-/MCR- und IOMMU-Lebenszyklus, authentifizierter GuC-Start, CT-/Scheduleranschluss, Shader-Dispatch und Ergebnis-/Fence-Nachweis. Erst danach kann die native Metal-/Display-Anbindung abgenommen werden. Ein zufällig leerer GGTT-Eintrag oder MMIO-Readback wird nicht als freier Adressraum beziehungsweise abgeschlossene TLB-Invalidation ausgegeben.

Quellenpin: Linux v6.17, `e5f0a698b34ed76002dc5cff3804a61c80233a7a`. Die [Forcewake-Implementierung](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_force_wake.c), [MMIO-Implementierung](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_mmio.c) und [Plattformregel](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_device_wa_oob.rules) sind per SHA-256 in der Referenzliste gebunden. Intel-MIT-Herkunft bleibt erhalten; eigener Code steht unter GPL-3.0-only.
