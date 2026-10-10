# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Execute real C++ memory/GuC code with hostile, boundary and reference inputs."""
import ctypes as c
import json
from pathlib import Path
import random
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parent
DLL = c.CDLL(str(Path(sys.argv.pop(1)).resolve()))

class Segment(c.Structure):
    _fields_ = [('address', c.c_uint64), ('bytes', c.c_uint64)]

class Mapping(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in ('token', 'gpuAddress', 'bytes')] + \
               [(n, c.c_uint32) for n in ('owner', 'patIndex', 'writable', 'reserved')]

class Arena(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in ('begin', 'end', 'nextToken', 'reservedBytes')] + \
               [('owner', c.c_uint32), ('count', c.c_uint32), ('reserved', c.c_uint32 * 2),
                ('mappings', Mapping * 32)]

class TablePlan(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in ('rootLevel', 'levels', 'leafEntries', 'totalPages')] + \
               [('pagesPerLevel', c.c_uint32 * 5), ('reserved', c.c_uint32)]

class Descriptor(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in ('head', 'tail', 'status')] + [('reserved', c.c_uint32 * 13)]

class Message(c.Structure):
    _fields_ = [('fence', c.c_uint32), ('count', c.c_uint32), ('data', c.c_uint32 * 255)]

class Firmware(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in ('fileBytes', 'ucodeOffset', 'ucodeBytes', 'rsaOffset',
                'rsaBytes', 'uploadBytes')] + [(n, c.c_uint32) for n in ('releaseVersion',
                'submissionVersion', 'privateDataBytes', 'referenceRecommended', 'moduleType',
                'moduleVendor', 'signatureAuthenticated', 'reserved')]

SIGNATURES = {
    'lb_xe2_page_entry': [c.c_uint32, c.c_uint64, c.c_uint32, c.c_uint32, c.POINTER(c.c_uint64)],
    'lb_xe2_page_indices': [c.c_uint64, c.POINTER(c.c_uint32)],
    'lb_xe2_table_plan': [c.c_uint64, c.c_uint64, c.POINTER(TablePlan)],
    'lb_vm_init': [c.POINTER(Arena), c.c_uint32, c.c_uint64, c.c_uint64],
    'lb_vm_reserve': [c.POINTER(Arena), c.c_uint32, c.c_uint64, c.c_uint32, c.c_uint32, c.POINTER(Mapping)],
    'lb_vm_release': [c.POINTER(Arena), c.c_uint32, c.c_uint64],
    'lb_vm_stage_ptes': [c.POINTER(Arena), c.c_uint32, c.c_uint64, c.POINTER(Segment), c.c_uint32,
                        c.POINTER(c.c_uint64), c.c_uint32],
    'lb_ct_encode': [c.c_uint32, c.POINTER(Message), c.POINTER(c.c_uint32), c.c_uint32],
    'lb_ct_decode': [c.c_uint32, c.POINTER(c.c_uint32), c.c_uint32, c.POINTER(Message)],
    'lb_ct_shadow_write': [c.c_uint32, c.POINTER(Descriptor), c.POINTER(c.c_uint32), c.c_uint32, c.POINTER(Message)],
    'lb_ct_shadow_read': [c.c_uint32, c.POINTER(Descriptor), c.POINTER(c.c_uint32), c.c_uint32, c.POINTER(Message)],
    'lb_guc_parse_css': [c.c_void_p, c.c_uint64, c.POINTER(Firmware)],
}
for name, arguments in SIGNATURES.items():
    getattr(DLL, name).argtypes = arguments
    getattr(DLL, name).restype = c.c_uint32

def message(direction=0, kind=0, count=3, fence=0xabcd):
    row = Message(fence, count)
    row.data[0] = direction << 31 | kind << 28 | 0x1234
    for i in range(1, count): row.data[i] = 0x98760000 + i
    return row

def css_fixture(release=0x462d02, full=False):
    # Fixed CSS layout, optional modulus/exponent absent in a truncated image.
    words = [0] * 32
    words[0], words[1], words[4], words[6] = 6, 39, 0x8086, 43
    words[7:10] = [4, 2, 1]
    words[16:18] = [release, 0x010203]
    words[30] = 4096
    header = bytearray(struct.pack('<32I', *words))
    header[44:64] = b'PRIVATE-BUILDER-TEXT!'[:20]
    return bytes(header) + b'\x37'*16 + b'\xa5'*16 + (b'\0'*12 if full else b'')

class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.arena = Arena()
        self.assertEqual(DLL.lb_vm_init(c.byref(self.arena), 42, 65536, (1 << 48) - 65536), 0)

    def reserve(self, bytes_=4096, pat=2, writable=1):
        result = Mapping()
        self.assertEqual(DLL.lb_vm_reserve(c.byref(self.arena), 42, bytes_, pat, writable, c.byref(result)), 0)
        return result

    def test_literal_xe2_pte_pde_ggtt_vectors(self):
        vectors = [(0, 0, 1, 0x12345003), (0, 2, 1, 0x12345013),
                   (0, 8, 0, 0x4000000012345001), (0, 20, 1, 0x2000000012345083),
                   (0, 31, 1, 0x600000001234509b), (1, 3, 1, 0x1234501b),
                   (2, 3, 1, 0x0030000012345001)]
        for kind, pat, rw, expected in vectors:
            output = c.c_uint64()
            self.assertEqual(DLL.lb_xe2_page_entry(kind, 0x12345000, pat, rw, c.byref(output)), 0)
            self.assertEqual(output.value, expected)

    def test_invalid_entry_flags_alignment_address_and_reserved_pat_preserve_output(self):
        cases = [(3, 4096, 2, 1), (0, 4097, 2, 1), (0, 1 << 46, 2, 1),
                 (0, (1 << 64) - 1, 2, 1), (0, 4096, 32, 1), (0, 4096, 2, 2),
                 (1, 4096, 4, 1), (1, 4096, 1, 0), (2, 4096, 4, 1), (2, 4096, 0, 0)]
        cases += [(0, 4096, pat, 1) for pat in range(16, 20)]
        for values in cases:
            output = c.c_uint64(0xabcdef)
            self.assertNotEqual(DLL.lb_xe2_page_entry(*values, c.byref(output)), 0)
            self.assertEqual(output.value, 0xabcdef)

    def test_five_level_indices_reconstruct_48bit_addresses(self):
        rng = random.Random(9021)
        for address in [0, 65536, (1 << 48) - 1, *(rng.randrange(1 << 48) for _ in range(3000))]:
            indices = (c.c_uint32 * 5)()
            self.assertEqual(DLL.lb_xe2_page_indices(address, indices), 0)
            reconstructed = sum(int(v) * (512 ** i) for i, v in enumerate(indices)) * 4096
            self.assertEqual(reconstructed, address - address % 4096)
            self.assertEqual(indices[4], 0)
        indices = (c.c_uint32 * 5)(*[99]*5)
        self.assertNotEqual(DLL.lb_xe2_page_indices(1 << 48, indices), 0)
        self.assertEqual(list(indices), [99]*5)

    def test_table_plan_crosses_each_directory_boundary(self):
        for boundary, pages in [(1 << 21, [2, 1, 1, 1, 1]),
                                 (1 << 30, [2, 2, 1, 1, 1]),
                                 (1 << 39, [2, 2, 2, 1, 1])]:
            plan = TablePlan()
            self.assertEqual(DLL.lb_xe2_table_plan(boundary-4096, 8192, c.byref(plan)), 0)
            self.assertEqual(list(plan.pagesPerLevel), pages)
            self.assertEqual((plan.levels, plan.rootLevel, plan.leafEntries, plan.totalPages),
                             (5, 4, 2, sum(pages)))

    def test_table_plan_rejects_overflow_and_unaligned_requests(self):
        for address, bytes_ in [(1 << 48, 4096), ((1 << 48)-4096, 8192),
                                ((1 << 64)-4096, 8192), (65537, 4096), (65536, 4097),
                                (65536, 0), (65536, 1024*1024+4096)]:
            output = TablePlan()
            c.memset(c.byref(output), 0xa5, c.sizeof(output))
            before = bytes(output)
            self.assertNotEqual(DLL.lb_xe2_table_plan(address, bytes_, c.byref(output)), 0)
            self.assertEqual(bytes(output), before)

    def test_guard_regions_and_arena_initialization_fail_without_mutation(self):
        for owner, begin, end in [(0, 65536, 131072), (42, 0, 131072), (42, 65537, 131072),
                                   (42, 65536, 1 << 48), (42, 65536, 65536), (42, 131072, 65536)]:
            before = bytes(self.arena)
            self.assertNotEqual(DLL.lb_vm_init(c.byref(self.arena), owner, begin, end), 0)
            self.assertEqual(bytes(self.arena), before)

    def test_foreign_owner_and_stale_token_cannot_free_or_stage(self):
        item = self.reserve()
        before = bytes(self.arena)
        self.assertEqual(DLL.lb_vm_release(c.byref(self.arena), 43, item.token), 5)
        self.assertEqual(bytes(self.arena), before)
        self.assertEqual(DLL.lb_vm_release(c.byref(self.arena), 42, item.token), 0)
        new = self.reserve()
        self.assertNotEqual(new.token, item.token)
        self.assertEqual(new.gpuAddress, item.gpuAddress)
        self.assertEqual(DLL.lb_vm_release(c.byref(self.arena), 42, item.token), 6)
        output = (c.c_uint64 * 1)(123)
        segments = (Segment * 1)(Segment(4096, 4096))
        self.assertEqual(DLL.lb_vm_stage_ptes(c.byref(self.arena), 43, new.token, segments, 1, output, 1), 5)
        self.assertEqual(DLL.lb_vm_stage_ptes(c.byref(self.arena), 42, item.token, segments, 1, output, 1), 6)
        self.assertEqual(output[0], 123)

    def test_fragmented_first_fit_reuses_gap_and_never_overlaps(self):
        items = [self.reserve(8192) for _ in range(3)]
        self.assertEqual(DLL.lb_vm_release(c.byref(self.arena), 42, items[1].token), 0)
        replacement = self.reserve(4096)
        self.assertEqual(replacement.gpuAddress, items[1].gpuAddress)
        following = self.reserve(4096)
        self.assertEqual(following.gpuAddress, replacement.gpuAddress+4096)
        self.assertEqual(self.arena.reservedBytes, 3*8192)

    def test_mapping_slots_byte_budget_and_va_exhaustion(self):
        for _ in range(32): self.reserve()
        output = Mapping()
        self.assertEqual(DLL.lb_vm_reserve(c.byref(self.arena), 42, 4096, 2, 1, c.byref(output)), 4)
        self.setUp()
        for _ in range(8): self.reserve(1024*1024)
        self.assertEqual(DLL.lb_vm_reserve(c.byref(self.arena), 42, 4096, 2, 1, c.byref(output)), 4)
        self.arena = Arena()
        self.assertEqual(DLL.lb_vm_init(c.byref(self.arena), 42, 65536, 69632), 0)
        self.reserve()
        self.assertEqual(DLL.lb_vm_reserve(c.byref(self.arena), 42, 4096, 2, 1, c.byref(output)), 4)

    def test_corrupt_arena_cannot_mutate_or_export_ptes(self):
        item = self.reserve()
        saved = bytes(self.arena)
        for field, value in [('count', 33), ('reservedBytes', 0), ('nextToken', 0), ('end', 0)]:
            c.memmove(c.byref(self.arena), saved, len(saved))
            setattr(self.arena, field, value)
            before = bytes(self.arena)
            self.assertEqual(DLL.lb_vm_release(c.byref(self.arena), 42, item.token), 7)
            self.assertEqual(bytes(self.arena), before)

    def test_token_exhaustion_does_not_wrap(self):
        self.arena.nextToken = (1 << 64) - 1
        before = bytes(self.arena)
        output = Mapping()
        self.assertEqual(DLL.lb_vm_reserve(c.byref(self.arena), 42, 4096, 2, 1, c.byref(output)), 4)
        self.assertEqual(bytes(self.arena), before)

    def test_reinitialization_cannot_discard_ownership_or_reuse_tokens(self):
        row = self.reserve()
        for active in (True, False):
            if not active:
                self.assertEqual(DLL.lb_vm_release(c.byref(self.arena), 42, row.token), 0)
            before = bytes(self.arena)
            self.assertEqual(DLL.lb_vm_init(c.byref(self.arena), 42, 65536, 131072), 7)
            self.assertEqual(bytes(self.arena), before)

    def test_dma_scatter_gather_stages_all_pages_and_preserves_tail(self):
        item = self.reserve(3*4096, pat=31)
        segments = (Segment * 2)(Segment(0x12345000, 8192), Segment(0x23000, 4096))
        output = (c.c_uint64 * 4)(*[0xa5]*4)
        self.assertEqual(DLL.lb_vm_stage_ptes(c.byref(self.arena), 42, item.token, segments, 2, output, 4), 0)
        self.assertEqual(list(output), [0x600000001234509b, 0x600000001234609b, 0x600000000002309b, 0xa5])

    def test_invalid_late_dma_segment_leaves_entire_output_untouched(self):
        item = self.reserve(8192)
        cases = [(4097, 4096), (8192, 0), (1 << 46, 4096), (4096, 4096),
                 ((1 << 46)-4096, 8192), (8192, 8192)]
        for address, size in cases:
            segments = (Segment * 2)(Segment(4096, 4096), Segment(address, size))
            output = (c.c_uint64 * 2)(111, 222)
            self.assertNotEqual(DLL.lb_vm_stage_ptes(c.byref(self.arena), 42, item.token, segments, 2, output, 2), 0)
            self.assertEqual(list(output), [111, 222])

    def test_missing_dma_pages_and_output_capacity_rejected(self):
        item = self.reserve(8192)
        segments = (Segment * 1)(Segment(4096, 4096))
        output = (c.c_uint64 * 2)(111, 222)
        self.assertNotEqual(DLL.lb_vm_stage_ptes(c.byref(self.arena), 42, item.token, segments, 1, output, 2), 0)
        segments[0].bytes = 8192
        self.assertEqual(DLL.lb_vm_stage_ptes(c.byref(self.arena), 42, item.token, segments, 1, output, 1), 4)
        self.assertEqual(list(output), [111, 222])

    def test_random_vm_lifetimes_match_independent_page_occupancy(self):
        rng = random.Random(4664)
        active, occupied = {}, set()
        for _ in range(3000):
            if active and (len(active) >= 28 or rng.randrange(3) == 0):
                token = rng.choice(list(active))
                addr, pages = active.pop(token)
                self.assertEqual(DLL.lb_vm_release(c.byref(self.arena), 42, token), 0)
                occupied.difference_update(range(addr//4096, addr//4096+pages))
            else:
                pages = rng.randrange(1, 33)
                item = self.reserve(pages*4096)
                new_pages = set(range(item.gpuAddress//4096, item.gpuAddress//4096+pages))
                self.assertFalse(occupied.intersection(new_pages))
                occupied.update(new_pages)
                active[item.token] = (item.gpuAddress, pages)
            self.assertEqual(self.arena.reservedBytes, len(occupied)*4096)
            self.assertEqual(self.arena.count, len(active))

    def test_literal_ctb_hxg_vector_and_zeroed_decode_tail(self):
        row = message()
        frame = (c.c_uint32 * 4)()
        self.assertEqual(DLL.lb_ct_encode(0, c.byref(row), frame, 4), 0)
        self.assertEqual(list(frame), [0xabcd0003, 0x1234, 0x98760001, 0x98760002])
        out = Message()
        c.memset(c.byref(out), 0xa5, c.sizeof(out))
        self.assertEqual(DLL.lb_ct_decode(0, frame, 4, c.byref(out)), 0)
        self.assertEqual((out.fence, out.count, list(out.data[:3])), (0xabcd, 3, list(row.data[:3])))
        self.assertEqual(list(out.data[3:]), [0]*252)

    def test_ctb_malformed_headers_wrong_origin_and_type_preserve_output(self):
        for header, hxg, length in [(0xabcd0101, 0x1234, 2), (0xabcd1001, 0x1234, 2),
                                    (0xabcd0003, 0x1234, 2), (0xabcd0000, 0x1234, 2),
                                    (0xabcd0001, 0x80001234, 2), (0xabcd0001, 0x40001234, 2)]:
            frame = (c.c_uint32 * 4)(header, hxg, 1, 2)
            out = Message()
            c.memset(c.byref(out), 0xa5, c.sizeof(out))
            before = bytes(out)
            self.assertNotEqual(DLL.lb_ct_decode(0, frame, length, c.byref(out)), 0)
            self.assertEqual(bytes(out), before)

    def test_ctb_encoding_overlapping_input_uses_stable_payload(self):
        row = message(count=255)
        frame = c.cast(c.byref(row), c.POINTER(c.c_uint32))
        expected = [0xabcd00ff, *list(row.data)]
        self.assertEqual(DLL.lb_ct_encode(0, c.byref(row), frame, 256), 0)
        self.assertEqual([frame[i] for i in range(256)], expected)

    def test_hxg_request_response_type_and_origin_matrix(self):
        for direction in (0, 1):
            for origin in (0, 1):
                for kind in range(8):
                    row = message(origin, kind)
                    out = (c.c_uint32 * 256)()
                    expected = origin == direction and kind in ({0, 2} if direction == 0 else {1, 3, 5, 6, 7})
                    self.assertEqual(DLL.lb_ct_encode(direction, c.byref(row), out, 256) == 0, expected)

    def test_ctb_maximum_payload_and_fence_boundaries(self):
        for fence in (0, 65535):
            row = message(count=255, fence=fence)
            frame = (c.c_uint32 * 256)()
            self.assertEqual(DLL.lb_ct_encode(0, c.byref(row), frame, 256), 0)
            decoded = Message()
            self.assertEqual(DLL.lb_ct_decode(0, frame, 256, c.byref(decoded)), 0)
            self.assertEqual(bytes(decoded), bytes(row))
        row.fence = 65536
        self.assertNotEqual(DLL.lb_ct_encode(0, c.byref(row), frame, 256), 0)
        row.fence = 1
        self.assertEqual(DLL.lb_ct_encode(0, c.byref(row), frame, 255), 3)

    def test_ctb_ring_wrap_at_last_dword(self):
        for size in (1024, 32768):
            desc, ring = Descriptor(size-1, size-1, 0), (c.c_uint32 * size)()
            row = message(1, 7)
            self.assertEqual(DLL.lb_ct_shadow_write(1, c.byref(desc), ring, size, c.byref(row)), 0)
            self.assertEqual(desc.tail, 3)
            self.assertEqual(ring[size-1], 0xabcd0003)
            output = Message()
            self.assertEqual(DLL.lb_ct_shadow_read(1, c.byref(desc), ring, size, c.byref(output)), 0)
            self.assertEqual(bytes(output), bytes(row))
            self.assertEqual(desc.head, desc.tail)
            self.assertEqual(DLL.lb_ct_shadow_read(1, c.byref(desc), ring, size, c.byref(output)), 4)

    def test_ctb_invalid_descriptor_and_ring_capacity_do_not_write(self):
        for head, tail, status, reserved, size in [(1024, 0, 0, 0, 1024), (0, 1024, 0, 0, 1024),
                                                  (0, 0, 1, 0, 1024), (0, 0, 8, 0, 1024),
                                                  (0, 0, 0, 1, 1024), (0, 0, 0, 0, 1000)]:
            desc = Descriptor(head, tail, status)
            desc.reserved[12] = reserved
            ring = (c.c_uint32 * 1024)()
            row = message()
            before = bytes(desc), bytes(ring)
            self.assertNotEqual(DLL.lb_ct_shadow_write(0, c.byref(desc), ring, size, c.byref(row)), 0)
            self.assertEqual((bytes(desc), bytes(ring)), before)

    def test_ctb_full_and_truncated_messages_preserve_cursors(self):
        desc, ring = Descriptor(0, 1022, 0), (c.c_uint32 * 1024)()
        before = bytes(desc), bytes(ring)
        row = message()
        self.assertEqual(DLL.lb_ct_shadow_write(0, c.byref(desc), ring, 1024, c.byref(row)), 3)
        self.assertEqual((bytes(desc), bytes(ring)), before)
        desc.head, desc.tail, ring[0] = 0, 2, 0xabcd0003
        before = bytes(desc)
        output = Message()
        self.assertEqual(DLL.lb_ct_shadow_read(0, c.byref(desc), ring, 1024, c.byref(output)), 5)
        self.assertEqual(bytes(desc), before)

    def test_random_ctb_ring_matches_fifo_without_message_loss(self):
        rng = random.Random(7070)
        desc, ring, expected = Descriptor(), (c.c_uint32 * 1024)(), []
        for i in range(4000):
            if expected and rng.randrange(2) == 0:
                output = Message()
                self.assertEqual(DLL.lb_ct_shadow_read(1, c.byref(desc), ring, 1024, c.byref(output)), 0)
                self.assertEqual(bytes(output), expected.pop(0))
            else:
                row = message(1, 7, rng.randrange(1, 256), i+1)
                status = DLL.lb_ct_shadow_write(1, c.byref(desc), ring, 1024, c.byref(row))
                self.assertIn(status, (0, 3))
                if status == 0:
                    expected.append(bytes(row))
        for wanted in expected:
            output = Message()
            self.assertEqual(DLL.lb_ct_shadow_read(1, c.byref(desc), ring, 1024, c.byref(output)), 0)
            self.assertEqual(bytes(output), wanted)
        self.assertEqual(desc.head, desc.tail)

    def parse(self, data):
        output = Firmware()
        raw = c.create_string_buffer(data)
        return DLL.lb_guc_parse_css(raw, len(data), c.byref(output)), output

    def test_css_truncated_optional_components_and_full_layout(self):
        for full in (False, True):
            status, output = self.parse(css_fixture(full=full))
            self.assertEqual(status, 0)
            self.assertEqual((output.ucodeOffset, output.ucodeBytes, output.rsaOffset, output.rsaBytes,
                              output.uploadBytes), (128, 16, 144, 16, 144))
            self.assertEqual((output.releaseVersion, output.submissionVersion), (0x462d02, 0x010203))
            self.assertEqual((output.signatureAuthenticated, output.reserved), (0, 0))
            self.assertNotIn(b'PRIVATE', bytes(output))

    def test_css_short_header_or_missing_required_ucode_and_rsa(self):
        data = css_fixture()
        for size in (0, 1, 127, 128, 143, 159):
            self.assertNotEqual(self.parse(data[:size])[0], 0)

    def test_css_hostile_size_fields_do_not_underflow_or_mutate_output(self):
        for offset, value in [(4, 0), (4, 0xffffffff), (24, 1), (24, 0xffffffff),
                              (28, 0), (28, 0xffffffff), (32, 0xffffffff), (36, 0xffffffff)]:
            data = bytearray(css_fixture())
            struct.pack_into('<I', data, offset, value)
            output = Firmware()
            c.memset(c.byref(output), 0xa5, c.sizeof(output))
            before = bytes(output)
            raw = c.create_string_buffer(bytes(data))
            self.assertNotEqual(DLL.lb_guc_parse_css(raw, len(data), c.byref(output)), 0)
            self.assertEqual(bytes(output), before)

    def test_css_release_floor_and_lunar_lake_recommendation_are_distinct(self):
        for release in (0, 0x451d02, 0x471d02, 0x461d01, 0x461c02):
            self.assertEqual(self.parse(css_fixture(release))[0], 4)
        status, output = self.parse(css_fixture(0x461d02))
        self.assertEqual((status, output.referenceRecommended), (0, 0))
        status, output = self.parse(css_fixture(0x462d02))
        self.assertEqual((status, output.referenceRecommended), (0, 1))

    def test_css_null_pointer_and_file_size_limit(self):
        output = Firmware()
        raw = c.create_string_buffer(css_fixture())
        self.assertEqual(DLL.lb_guc_parse_css(None, 128, c.byref(output)), 1)
        self.assertEqual(DLL.lb_guc_parse_css(raw, len(raw), None), 1)
        self.assertEqual(DLL.lb_guc_parse_css(raw, 16*1024*1024+1, c.byref(output)), 1)

    def test_null_memory_and_transport_arguments(self):
        self.assertNotEqual(DLL.lb_vm_init(None, 42, 65536, 131072), 0)
        self.assertNotEqual(DLL.lb_xe2_page_entry(0, 4096, 2, 1, None), 0)
        self.assertNotEqual(DLL.lb_xe2_page_indices(4096, None), 0)
        self.assertNotEqual(DLL.lb_ct_decode(0, None, 2, c.byref(Message())), 0)
        self.assertNotEqual(DLL.lb_ct_encode(0, None, (c.c_uint32*256)(), 256), 0)
        self.assertNotEqual(DLL.lb_ct_shadow_read(1, None, (c.c_uint32*1024)(), 1024, c.byref(Message())), 0)

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FoundationTests))
    report = {'test_type': 'Executed C++ Xe2 PTE/VA/DMA staging, GuC CSS and CPU-shadow CTB codec; synthetic addresses/firmware/messages',
              'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'successful': result.wasSuccessful(),
              'synthetic_inputs': True, 'actual_dma_or_gpu_binding': False,
              'firmware_uploaded': False, 'guc_hardware_messages_sent': False,
              'native_macos_hardware_tests': 0}
    (ROOT/'gpu-foundation-test-report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
