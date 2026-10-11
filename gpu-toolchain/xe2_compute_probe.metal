// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Same integer operation and input/output/count interface as xe2_integer_probe.cl.
#include <metal_stdlib>
using namespace metal;

kernel void lunarbridge_xe2_probe(device const uint *input [[buffer(0)]],
                                device uint *output [[buffer(1)]],
                                constant uint &count [[buffer(2)]],
                                uint index [[thread_position_in_grid]]) {
    if (index < count) {
        const uint value = input[index];
        output[index] = (value * 1664525u + 1013904223u)
                      ^ (0xA5A55A5Au + index);
    }
}
