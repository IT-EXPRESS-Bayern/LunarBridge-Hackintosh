# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Turn bounded LunarBridge diagnostics into a German triage report."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lb_wire import EVENTS, POLICIES

def findings(report):
    result = []
    def add(level, code, message, source):
        if not any(r['code'] == code for r in result):
            result.append({'level': level, 'code': code, 'message': message, 'source': source})
    if report.get('synthetic'):
        add('info', 'FIXTURE', 'Synthetische Testdaten; kein macOS-Hardwarebefund.', 'tests')
    if not report.get('snapshot'):
        add('warning', 'NO_SNAPSHOT', 'Kein nativer Snapshot verfügbar. Kext-Laden, Matching und Client-Zugriff prüfen.',
            'native/Info.plist; native/LunarBridgeDriver.cpp')
    else:
        snapshot = report['snapshot']
        if snapshot.get('policy') == 1:
            add('info', 'MMIO_DEFAULT_OFF', 'Registerzugriff ist im Standardmodus bewusst gesperrt.', 'native/Info.plist')
        elif snapshot.get('policy', 0) != 0:
            add('warning', 'MMIO_PRECONDITION', 'Registerzugriff abgewiesen: ' +
                POLICIES.get(snapshot['policy'], 'unbekannte Vorbedingung') + '. Keine automatische Geräteänderung.',
                'gpu-prototype/lb_diagnostics.cpp: lb_mmio_preflight')
    if report.get('trace_overwritten', 0):
        add('warning', 'TRACE_TRUNCATED', 'Ältere Ringpuffereinträge wurden überschrieben; Kernelmeldungen ergänzen.',
            'gpu-prototype/lb_diagnostics.cpp: lb_trace_read')
    if report.get('trace_error'):
        add('error', 'TRACE_READ_FAILED', 'Trace-Lesen fehlgeschlagen; der bisherige Snapshot bleibt erhalten.',
            'tools/collect_macos.py; gpu-prototype/lb_diagnostics.h')
    if isinstance(report.get('cpu_memory_test'), dict) and report['cpu_memory_test'].get('state') == 'failed':
        add('error', 'CPU_ROUNDTRIP_FAILED', 'Nativer CPU-Speichertest fehlgeschlagen; Status im JSON-Bericht prüfen.',
            'tools/collect_macos.py: cpu_memory_roundtrip')
    if report.get('iokit_error'):
        add('error', 'IOKIT_FAILED', 'IOKit-Zugriff fehlgeschlagen; Selectorstatus und ABI im JSON-Bericht prüfen.',
            'tools/collect_macos.py')
    rows = report.get('trace', []) + report.get('kernel_events', [])
    ids = {row.get('event') for row in rows}
    if 4 in ids:
        add('error', 'BAR_MAP_FAILED', 'BAR-Mapping schlug fehl. BAR-Bereich, PCI-Speicherfreigabe und D0-Zustand prüfen.',
            'native/LunarBridgeDriver.cpp: LunarBridgePCI::start')
    if 6 in ids:
        add('error', 'GMD_INVALID', 'GMD-Werte wurden verworfen. Rohwerte und Geräte-/Energiezustand vor dem Lesen prüfen.',
            'gpu-prototype/lnl_platform.cpp: lnl_decode_gmd')
    if 24 in ids:
        add('error', 'CPU_BUFFER_FAILED', 'CPU-Pufferanforderung fehlgeschlagen. Angeforderte Größe, Budget und IOKit-Status prüfen.',
            'native/LunarBridgeDriver.cpp: LunarBridgeUserClient::perform')
    if 26 in ids:
        add('warning', 'ABI_REJECTED', 'Eine Client-Anfrage wurde abgewiesen. Selector, ABI und Argumentgrößen abgleichen.',
            'gpu-prototype/lb_diagnostics.h; tools/lb_wire.py')
    if 29 in ids:
        add('warning', 'CLIENT_REJECTED', 'Client wurde abgewiesen. Administratorrecht und Verbindungslimit prüfen.',
            'native/LunarBridgeDriver.cpp: LunarBridgeUserClient::start')
    if 27 in ids:
        add('info', 'GPU_NOT_IMPLEMENTED', 'GPU-Ausführung ist in dieser Version nicht implementiert; Unsupported ist erwartet.',
            'native/LunarBridgeDriver.cpp: LBSubmit')
    if 1 in ids and 7 not in ids:
        add('warning', 'START_NOT_COMPLETED', 'Start wurde erfasst, Bereitschaft fehlt im verfügbaren Trace. Startpfad oder Trace-Verlust prüfen.',
            'native/LunarBridgeDriver.cpp: LunarBridgePCI::start')
    if report.get('snapshot', {}).get('claims_metal'):
        add('warning', 'METAL_CLAIM_UNVERIFIED', 'Ein Capability-Bit ersetzt keinen bestandenen Metal-Ausführungstest.',
            'gpu-prototype/Metal-Minimaltest.mm')
    return result

def markdown(report):
    rows = report.get('trace', []) + report.get('kernel_events', [])
    lines = ['# LunarBridge-Diagnose', '',
             'Quelle: ' + ('synthetische Testdaten' if report.get('synthetic') else 'nativer Sammlerbericht'), '',
             'Dieser Bericht belegt keine Metal-Beschleunigung oder fehlerfreie Boot-/Display-Funktion.', '',
             '## Befunde', '']
    items = findings(report)
    for item in items:
        lines += [f"- **{item['level']} / {item['code']}**: {item['message']} Quelle: `{item['source']}`."]
    if not items: lines += ['Keine der dokumentierten Fehlerbedingungen im verfügbaren Bericht erkannt.']
    lines += ['', '## Ereignisse', '', '| Sequenz | Ereignis | Status | Kontext | Argumente |',
              '| --- | --- | --- | --- | --- |']
    unique = {(r.get('sequence'), r.get('event'), r.get('context'), r.get('status')): r for r in rows}
    for r in sorted(unique.values(), key=lambda r: r.get('sequence', 0))[-256:]:
        lines += [f"| {r.get('sequence', 0)} | {EVENTS.get(r.get('event'), 'unknown_event')} | "
                  f"{r.get('status', 'unknown')} | {r.get('context', 0)} | "
                  f"{r.get('arg0', 0)}, {r.get('arg1', 0)} |"]
    lines += ['', 'GPU-Ausführung, Metal, Display, Standby und Firmware-Secure-Boot müssen jeweils separat geprüft werden.', '']
    return '\n'.join(lines)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding='utf-8-sig'))
    result = markdown(report)
    if args.out: args.out.write_text(result, encoding='utf-8', newline='\n')
    else: print(result)
