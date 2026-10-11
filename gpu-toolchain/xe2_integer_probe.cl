// SPDX-License-Identifier: GPL-3.0-only
// Own bounded integer compute probe. No display, graphics API, or driver control.
__kernel void lunarbridge_xe2_probe(__global const uint *input,
                                   __global uint *output,
                                   const uint count) {
    const size_t index = get_global_id(0);
    if (index < count) {
        const uint value = input[index];
        output[index] = (value * 1664525u + 1013904223u)
                      ^ (0xA5A55A5Au + (uint)index);
    }
}
