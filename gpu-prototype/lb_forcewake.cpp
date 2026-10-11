// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#include "lb_forcewake.h"

static constexpr uint32_t Magic = 0x4c424657;

static bool valid(const LBForceWakeState *s) {
    if (!s || s->magic != Magic || s->references > 65535 || s->owned > 1 ||
        s->awake > 1 || s->unresolved > 1 || s->reserved[0] || s->reserved[1] || s->reserved[2])
        return false;
    if (s->references) return s->owned && s->awake && !s->unresolved;
    return s->owned ? (!s->awake && s->unresolved) : (!s->awake && !s->unresolved);
}

static bool validIO(const LBRegisterIO *io) {
    return io && io->context && io->read32 && io->write32 && io->sleepUs && io->nowUs;
}

static uint32_t read(LBForceWakeState *s, const LBRegisterIO *io, uint32_t offset, uint32_t *value) {
    if (s->reads == UINT32_MAX) return LBWakeBadState;
    if (!io->read32(io->context, offset, value)) return LBWakeIOFailed;
    ++s->reads;
    if (offset == LB_FW_ACK) s->lastAck = *value;
    return *value == UINT32_MAX ? LBWakeInvalidMMIO : LBWakeOK;
}

static uint32_t write(LBForceWakeState *s, const LBRegisterIO *io, uint32_t value) {
    if (s->writes == UINT32_MAX) return LBWakeBadState;
    if (!io->write32(io->context, LB_FW_CONTROL, value)) return LBWakeIOFailed;
    ++s->writes;
    return LBWakeOK;
}

static uint32_t waitAck(LBForceWakeState *s, const LBRegisterIO *io, bool awake) {
    const uint64_t begin = io->nowUs(io->context);
    uint64_t previous = begin;
    // The iteration bound also protects against a stalled or dishonest clock.
    for (uint32_t i = 0; i < 64; ++i) {
        const uint64_t now = io->nowUs(io->context);
        if (now < previous || now < begin) return LBWakeClockFailed;
        previous = now;
        if (now - begin >= LB_FW_TIMEOUT_US) return LBWakeTimeout;
        if (s->polls == UINT32_MAX) return LBWakeBadState;
        uint32_t ack = 0;
        const uint32_t status = read(s, io, LB_FW_ACK, &ack);
        ++s->polls;
        if (status != LBWakeOK) return status;
        const uint64_t afterRead = io->nowUs(io->context);
        if (afterRead < previous || afterRead < begin) return LBWakeClockFailed;
        previous = afterRead;
        if (afterRead - begin >= LB_FW_TIMEOUT_US) return LBWakeTimeout;
        if ((ack & 1u) == (awake ? 1u : 0u)) return LBWakeOK;
        if (!io->sleepUs(io->context, 1000)) return LBWakeIOFailed;
    }
    return LBWakeTimeout;
}

extern "C" uint32_t lb_forcewake_init(LBForceWakeState *s) {
    if (!s) return LBWakeBadState;
    if (s->magic || s->references || s->owned || s->awake || s->unresolved ||
        s->reads || s->writes || s->polls || s->lastAck ||
        s->reserved[0] || s->reserved[1] || s->reserved[2])
        return LBWakeBadState; // Never overwrite an outstanding lease.
    s->magic = Magic;
    return LBWakeOK;
}

extern "C" uint32_t lb_forcewake_get(LBForceWakeState *s, const LBRegisterIO *io) {
    if (!valid(s)) return LBWakeBadState;
    if (!validIO(io)) return LBWakeBadIO;
    if (s->unresolved || s->references == 65535) return LBWakeBusy;
    if (s->references) { ++s->references; return LBWakeOK; }
    uint32_t control = 0, ack = 0;
    uint32_t status = read(s, io, LB_FW_CONTROL, &control);
    if (status != LBWakeOK) return status;
    status = read(s, io, LB_FW_ACK, &ack);
    if (status != LBWakeOK) return status;
    // Do not steal the kernel requestor from another owner. Other bits are
    // left untouched by the masked request/release writes below.
    if ((control | ack) & 1u) return LBWakeBusy;
    s->owned = s->unresolved = 1;
    status = write(s, io, LB_FW_REQUEST);
    if (status != LBWakeOK) return status;
    status = waitAck(s, io, true);
    if (status == LBWakeOK) {
        s->references = s->awake = 1;
        s->unresolved = 0;
    }
    return status;
}

extern "C" uint32_t lb_forcewake_put(LBForceWakeState *s, const LBRegisterIO *io) {
    if (!valid(s)) return LBWakeBadState;
    if (!validIO(io)) return LBWakeBadIO;
    if (!s->owned) return LBWakeIdle;
    if (s->references > 1) { --s->references; return LBWakeOK; }
    s->references = s->awake = 0;
    s->unresolved = 1;
    uint32_t status = write(s, io, LB_FW_RELEASE);
    if (status != LBWakeOK) return status;
    status = waitAck(s, io, false);
    if (status == LBWakeOK) s->owned = s->unresolved = 0;
    return status;
}

extern "C" int lb_forcewake_drained(const LBForceWakeState *s) {
    return valid(s) && !s->owned && !s->references;
}
