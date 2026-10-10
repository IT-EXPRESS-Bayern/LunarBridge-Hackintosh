// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Future native macOS acceptance test. Not built or executed on Windows.
// Build on macOS: clang++ -std=c++17 -fobjc-arc Metal-Minimaltest.mm
//                 -framework Foundation -framework Metal -o metal-minimaltest
// Checks actual command completion and computed data, not only an adapter name.
// Passing does not establish display acceleration, sleep/wake or performance.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <cstdio>
#include <cstring>

int main() {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) { std::fprintf(stderr, "FAIL: no default Metal device\n"); return 1; }
        std::printf("Metal device: %s; registry ID: %llu\n", device.name.UTF8String,
                    static_cast<unsigned long long>(device.registryID));
        NSError *error = nil;
        NSString *source = @"#include <metal_stdlib>\nusing namespace metal;\n"
            "kernel void add(device const uint *a [[buffer(0)]],"
            "device const uint *b [[buffer(1)]],device uint *out [[buffer(2)]],"
            "uint i [[thread_position_in_grid]]) { out[i] = a[i] + b[i]; }";
        id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
        id<MTLFunction> function = [library newFunctionWithName:@"add"];
        id<MTLComputePipelineState> pipeline = function ?
            [device newComputePipelineStateWithFunction:function error:&error] : nil;
        if (!pipeline) { std::fprintf(stderr, "FAIL: pipeline: %s\n", error.description.UTF8String); return 2; }
        const NSUInteger count = 65536, bytes = count * sizeof(uint32_t);
        id<MTLBuffer> a = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
        id<MTLBuffer> b = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
        id<MTLBuffer> out = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
        if (!a || !b || !out) { std::fprintf(stderr, "FAIL: buffers\n"); return 3; }
        auto *av = static_cast<uint32_t *>(a.contents);
        auto *bv = static_cast<uint32_t *>(b.contents);
        auto *ov = static_cast<uint32_t *>(out.contents);
        for (NSUInteger i = 0; i < count; ++i) { av[i] = uint32_t(i * 17); bv[i] = uint32_t(i ^ 0x1234); ov[i] = 0xdeadbeef; }
        id<MTLCommandQueue> queue = [device newCommandQueue];
        id<MTLCommandBuffer> command = [queue commandBuffer];
        id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
        if (!encoder) { std::fprintf(stderr, "FAIL: encoder\n"); return 4; }
        [encoder setComputePipelineState:pipeline];
        [encoder setBuffer:a offset:0 atIndex:0]; [encoder setBuffer:b offset:0 atIndex:1];
        [encoder setBuffer:out offset:0 atIndex:2];
        const NSUInteger width = MIN(pipeline.maxTotalThreadsPerThreadgroup, NSUInteger(64));
        if (!width) { std::fprintf(stderr, "FAIL: dispatch width\n"); return 5; }
        [encoder dispatchThreads:MTLSizeMake(count, 1, 1) threadsPerThreadgroup:MTLSizeMake(width, 1, 1)];
        [encoder endEncoding]; [command commit]; [command waitUntilCompleted];
        if (command.status != MTLCommandBufferStatusCompleted) {
            std::fprintf(stderr, "FAIL: command: %s\n", command.error.description.UTF8String); return 6;
        }
        for (NSUInteger i = 0; i < count; ++i)
            if (ov[i] != uint32_t(av[i] + bv[i])) {
                std::fprintf(stderr, "FAIL: output at %lu\n", static_cast<unsigned long>(i)); return 7;
            }
        std::printf("PASS: %lu correct compute outputs; GPU time %.6f s\n",
                    static_cast<unsigned long>(count), command.GPUEndTime - command.GPUStartTime);
        std::puts("Metal command/data test only. Confirm the real Arc backend separately; display not tested.");
        return 0;
    }
}
