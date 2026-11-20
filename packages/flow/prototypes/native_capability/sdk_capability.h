// Source prototype only. No built/exported DLL, loader or runtime caller.
#pragma once
#include <Windows.h>
#include <UIAutomation.h>
#include <UIAutomationCoreApi.h>
#include <cstdint>

namespace openadapt::capability_prototype {
inline constexpr std::uint32_t kSourceAbi = 1;
enum class Pattern : std::uint32_t { Toggle, SelectionItem, Value };
enum class Capability : std::uint32_t { Unknown, Supported, ExplicitlyUnsupported };
enum class Stage : std::uint32_t {
    BeforeAcquire, BeforeQueryInterface, AfterQueryInterface,
    AfterRelease, AfterUnsupported
};
enum class Reason : std::uint32_t {
    InvalidRequest, GuardOrScope, Acquisition, ContradictoryOutput,
    NormalNull, QueryInterface, Supported, ExplicitlyUnsupported
};
struct Scope {
    std::uint8_t identity[32];
    std::uint64_t generation;
};
// Future implementation must freshly check the existing supported raw
// sensitivity + fixed element/window identity/deadline/protocol at EACH call.
// This callback is a source seam, not a trusted caller boolean in any runtime.
// It must itself avoid automatic error-info conversion and extra content reads.
using Checkpoint = bool (__cdecl *)(void*, const Scope&, Stage) noexcept;
struct Observation {
    std::uint32_t source_abi = kSourceAbi;
    Capability capability = Capability::Unknown;
    Reason reason = Reason::InvalidRequest;
    std::int32_t acquire_hresult = 0;
    std::int32_t query_hresult = 0;
    bool acquire_attempted = false;
    bool query_attempted = false;
    bool acquire_nonnull = false;
    bool query_nonnull = false;
};
// Caller owns a live, already-bound element reference in the correct apartment
// through the final checkpoint; callbacks must not release/invalidate it. No Element re-find, AddRef/marshaler or output
// interface escapes this operation. No getter is implemented by this shim.
Observation observe_capability(
    IUIAutomationElement* borrowed_element, Pattern pattern,
    std::uint32_t source_abi, Scope expected_scope,
    Checkpoint checkpoint, void* checkpoint_context) noexcept;
}  // namespace openadapt::capability_prototype
