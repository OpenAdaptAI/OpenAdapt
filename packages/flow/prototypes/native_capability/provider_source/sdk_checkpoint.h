// Fixture-only SOURCE candidate. No runtime factory or qualification grant.
#pragma once
#include "../sdk_capability.h"
#include <array>
#include <cstdint>

namespace openadapt::provider_source {
enum class Privacy { Unknown, NonSensitive, Sensitive };
enum class GuardReason {
    None, Deadline, OwnerUnavailable, OwnerMismatch, ElementUnavailable,
    RuntimeUnavailable, ScopeChanged, Sensitive, PrivacyUnknown, CleanupUnknown
};
enum class GuardEvent { Enter, PrivacyObserved, Accepted, Refused, PrivacyBegin };
struct PrivacyObservation {
    HRESULT status = E_UNEXPECTED;
    VARTYPE kind = VT_EMPTY;
    bool boolean_observed = false;
    VARIANT_BOOL boolean = 0;
};
struct RuntimeId {
    std::array<LONG, 64> values{};
    std::size_t size = 0;
    bool operator==(const RuntimeId& other) const noexcept;
};
struct ObservedScope {
    HWND hwnd = nullptr;
    DWORD pid = 0, tid = 0, session = 0;
    FILETIME creation{};
    std::uint64_t fixture_generation = 0;
    RuntimeId runtime;
    bool operator==(const ObservedScope& other) const noexcept;
};
struct ExpectedOwner {
    HWND issued_hwnd = nullptr;
    DWORD pid = 0, session = 0;
    std::uint64_t fixture_generation = 0;
};
// Reads actual own-window lifecycle state. This is an independent fixture
// oracle, NOT an external-provider or business-record authority mechanism.
using GenerationReader = std::uint64_t (*)(void*, HWND) noexcept;
using GuardTrace = void (*)(void*, unsigned, GuardEvent, GuardReason, HRESULT, const PrivacyObservation*) noexcept;
Privacy classify_privacy(HRESULT status, VARTYPE kind, VARIANT_BOOL value) noexcept;

class Checkpoint {
    HANDLE process_ = nullptr;  // Retained duplicate of this fixture process.
    IUIAutomationElement* element_ = nullptr;  // Own AddRef held through all probes.
    ObservedScope baseline_{};
    ExpectedOwner expected_{};
    GenerationReader generation_ = nullptr;
    void* generation_context_ = nullptr;
    GuardTrace trace_ = nullptr;
    void* trace_context_ = nullptr;
    ULONGLONG deadline_ = 0;
    GuardReason reason_ = GuardReason::None;
    unsigned stage_ = 5;  // 0..4 frozen SDK checkpoints; 5 bootstrap.
    PrivacyObservation last_privacy_{};
    bool native_owner(ObservedScope&) noexcept;
    bool runtime(ObservedScope&) noexcept;
    bool nonsensitive() noexcept;
    bool refuse(GuardReason, HRESULT = S_OK) noexcept;
public:
    Checkpoint() = default;
    Checkpoint(const Checkpoint&) = delete;
    Checkpoint& operator=(const Checkpoint&) = delete;
    ~Checkpoint() noexcept;
    bool bind(IUIAutomation*, ExpectedOwner, GenerationReader, void*, GuardTrace, void*) noexcept;
    bool check() noexcept;
    void expire_for_owned_control() noexcept { deadline_ = 0; }
    IUIAutomationElement* borrowed_element() const noexcept { return element_; }
    const ObservedScope& observed_scope() const noexcept { return baseline_; }
    GuardReason reason() const noexcept { return reason_; }
    const PrivacyObservation& privacy_observation() const noexcept { return last_privacy_; }
    openadapt::capability_prototype::Scope sdk_scope() const noexcept;
    static bool __cdecl callback(void*, const openadapt::capability_prototype::Scope&,
        openadapt::capability_prototype::Stage) noexcept;
};
}  // namespace openadapt::provider_source
