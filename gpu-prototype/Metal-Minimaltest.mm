// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Bounded public-Metal compute acceptance test; no driver or policy changes.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <dispatch/dispatch.h>
#include <cstdint>
#include <cstdio>
#include <cstring>

#if !defined(__x86_64__)
#error This acceptance binary must be built for x86_64.
#endif

struct Evidence {
    bool provider = false, shader = false, buffers = false, submitted = false;
    bool completionHandler = false, completed = false;
    uint32_t checked = 0, mismatches = 0;
};

static int emit(NSString *context, const Evidence &e, NSString *state,
                NSString *code, int exitCode) {
    const bool verified = [state isEqualToString:@"passed"] && e.provider &&
        e.shader && e.buffers && e.submitted && e.completionHandler &&
        e.completed && e.checked == 65536 && e.mismatches == 0;
    NSDictionary *report = @{
        @"schema": @1, @"context": context, @"state": state, @"code": code,
        @"architecture": @"x86_64", @"value_count": @65536,
        @"gpu_provider_observed": @(e.provider), @"shader_compiled": @(e.shader),
        @"buffers_created": @(e.buffers), @"command_submitted": @(e.submitted),
        @"completion_handler_observed": @(e.completionHandler),
        @"command_completed": @(e.completed), @"results_checked": @(e.checked),
        @"mismatches": @(e.mismatches), @"gpu_execution_verified": @(verified),
        @"cpu_fallback_used": @NO, @"target_hardware_identity_verified": @NO,
        @"arc130v_backend_verified": @NO, @"display_acceleration_verified": @NO,
        @"native_driver_support_verified": @NO
    };
    NSData *encoded = [NSJSONSerialization dataWithJSONObject:report options:0 error:nullptr];
    if (!encoded || std::fwrite(encoded.bytes, 1, encoded.length, stdout) != encoded.length) return 1;
    std::fputc('\n', stdout);
    return exitCode;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        Evidence e{};
        NSString *context = @"ci-host";
        if (argc != 3 || std::strcmp(argv[1], "--context") != 0 ||
            (std::strcmp(argv[2], "ci-host") != 0 && std::strcmp(argv[2], "target-probe") != 0))
            return emit(context, e, @"failed", @"INVALID_ARGUMENTS", 1);
        context = [NSString stringWithUTF8String:argv[2]];
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) return emit(context, e, @"unverified", @"NO_METAL_PROVIDER", 2);
        e.provider = true;
        // Never emit names, RegistryIDs, paths, NSError descriptions or buffers.
        NSString *source = @"#include <metal_stdlib>\nusing namespace metal;\n"
            "kernel void add(device const uint *a [[buffer(0)]],"
            "device const uint *b [[buffer(1)]],device uint *out [[buffer(2)]],"
            "uint i [[thread_position_in_grid]]) { if(i < 65536) out[i] = a[i] + b[i]; }";
        NSError *error = nil;
        id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
        if (!library) return emit(context, e, @"failed", @"SHADER_LIBRARY_FAILED", 1);
        e.shader = true;
        id<MTLFunction> function = [library newFunctionWithName:@"add"];
        if (!function) return emit(context, e, @"failed", @"SHADER_ENTRY_MISSING", 1);
        id<MTLComputePipelineState> pipeline =
            [device newComputePipelineStateWithFunction:function error:&error];
        if (!pipeline) return emit(context, e, @"failed", @"PIPELINE_CREATION_FAILED", 1);
        constexpr NSUInteger count = 65536, bytes = count * sizeof(uint32_t);
        id<MTLBuffer> a = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
        id<MTLBuffer> b = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
        id<MTLBuffer> output = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
        if (!a || !b || !output) return emit(context, e, @"failed", @"BUFFER_ALLOCATION_FAILED", 1);
        e.buffers = true;
        auto *av = static_cast<uint32_t *>(a.contents);
        auto *bv = static_cast<uint32_t *>(b.contents);
        auto *ov = static_cast<uint32_t *>(output.contents);
        if (!av || !bv || !ov) return emit(context, e, @"failed", @"BUFFER_CONTENTS_UNAVAILABLE", 1);
        for (NSUInteger i = 0; i < count; ++i) {
            av[i] = static_cast<uint32_t>(i * 17);
            bv[i] = static_cast<uint32_t>(i ^ 0x1234);
            ov[i] = 0xdeadbeef;
        }
        id<MTLCommandQueue> queue = [device newCommandQueue];
        if (!queue) return emit(context, e, @"failed", @"COMMAND_QUEUE_FAILED", 1);
        id<MTLCommandBuffer> command = [queue commandBuffer];
        if (!command) return emit(context, e, @"failed", @"COMMAND_BUFFER_FAILED", 1);
        id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
        if (!encoder) return emit(context, e, @"failed", @"ENCODER_CREATION_FAILED", 1);
        const NSUInteger width = MIN(pipeline.maxTotalThreadsPerThreadgroup, NSUInteger(64));
        if (!width) return emit(context, e, @"failed", @"DISPATCH_WIDTH_INVALID", 1);
        [encoder setComputePipelineState:pipeline];
        [encoder setBuffer:a offset:0 atIndex:0];
        [encoder setBuffer:b offset:0 atIndex:1];
        [encoder setBuffer:output offset:0 atIndex:2];
        [encoder dispatchThreads:MTLSizeMake(count, 1, 1)
            threadsPerThreadgroup:MTLSizeMake(width, 1, 1)];
        [encoder endEncoding];
        dispatch_semaphore_t done = dispatch_semaphore_create(0);
        [command addCompletedHandler:^(id<MTLCommandBuffer>) { dispatch_semaphore_signal(done); }];
        e.submitted = true;
        [command commit];
        if (dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 20 * NSEC_PER_SEC)) != 0)
            return emit(context, e, @"failed", @"COMMAND_TIMEOUT", 1);
        e.completionHandler = true;
        if (command.status != MTLCommandBufferStatusCompleted)
            return emit(context, e, @"failed", @"COMMAND_FAILED", 1);
        e.completed = true;
        for (NSUInteger i = 0; i < count; ++i) {
            ++e.checked;
            if (ov[i] != static_cast<uint32_t>(av[i] + bv[i])) ++e.mismatches;
        }
        if (e.mismatches) return emit(context, e, @"failed", @"RESULT_MISMATCH", 1);
        return emit(context, e, @"passed", @"COMPUTE_VERIFIED", 0);
    }
}
