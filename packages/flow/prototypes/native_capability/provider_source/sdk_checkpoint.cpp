#include "sdk_checkpoint.h"
#include <cstring>

namespace openadapt::provider_source {
bool RuntimeId::operator==(const RuntimeId& other) const noexcept {
    if (size != other.size) return false;
    for (std::size_t i = 0; i < size; ++i) if (values[i] != other.values[i]) return false;
    return true;
}
bool ObservedScope::operator==(const ObservedScope& o) const noexcept {
    return hwnd == o.hwnd && pid == o.pid && tid == o.tid && session == o.session &&
        creation.dwLowDateTime == o.creation.dwLowDateTime &&
        creation.dwHighDateTime == o.creation.dwHighDateTime &&
        fixture_generation == o.fixture_generation && runtime == o.runtime;
}
Privacy classify_privacy(HRESULT status, VARTYPE kind, VARIANT_BOOL value) noexcept {
    if (status != S_OK || kind != VT_BOOL) return Privacy::Unknown;
    if (value == VARIANT_FALSE) return Privacy::NonSensitive;
    if (value == VARIANT_TRUE) return Privacy::Sensitive;
    return Privacy::Unknown;
}
bool Checkpoint::refuse(GuardReason reason, HRESULT status) noexcept {
    if (reason_ == GuardReason::None) {
        reason_ = reason;
        if (trace_) trace_(trace_context_, stage_, GuardEvent::Refused, reason, status, nullptr);
    }
    return false;
}
Checkpoint::~Checkpoint() noexcept {
    if (element_) element_->Release();
    if (process_) CloseHandle(process_);
}
bool Checkpoint::native_owner(ObservedScope& observed) noexcept {
    if (reason_ != GuardReason::None) return false;
    if (GetTickCount64() >= deadline_) return refuse(GuardReason::Deadline);
    if (!process_ || WaitForSingleObject(process_, 0) != WAIT_TIMEOUT)
        return refuse(GuardReason::OwnerUnavailable);
    observed.pid = GetProcessId(process_);
    FILETIME exit{}, kernel{}, user{};
    if (!observed.pid || !GetProcessTimes(process_, &observed.creation, &exit, &kernel, &user) ||
        !ProcessIdToSessionId(observed.pid, &observed.session))
        return refuse(GuardReason::OwnerUnavailable);
    observed.hwnd = expected_.issued_hwnd;
    DWORD window_pid = 0;
    observed.tid = GetWindowThreadProcessId(observed.hwnd, &window_pid);
    if (!observed.tid || !IsWindow(observed.hwnd) || window_pid != observed.pid ||
        observed.pid != expected_.pid || observed.session != expected_.session)
        return refuse(GuardReason::OwnerMismatch);
    observed.fixture_generation = generation_(generation_context_, observed.hwnd);
    if (!observed.fixture_generation || observed.fixture_generation != expected_.fixture_generation)
        return refuse(GuardReason::ScopeChanged);
    return true;
}
bool Checkpoint::nonsensitive() noexcept {
    if (reason_ != GuardReason::None) return false;
    if (GetTickCount64() >= deadline_) return refuse(GuardReason::Deadline);
    if (trace_) trace_(trace_context_, stage_, GuardEvent::PrivacyBegin,
        GuardReason::None, S_OK, nullptr);
    VARIANT raw; VariantInit(&raw);
    const HRESULT status = element_->GetCurrentPropertyValueEx(UIA_IsPasswordPropertyId, TRUE, &raw);
    // Never inspect a BSTR/object/array or coerce another numeric type.
    last_privacy_ = {status, raw.vt, status == S_OK && raw.vt == VT_BOOL, 0};
    if (last_privacy_.boolean_observed) last_privacy_.boolean = raw.boolVal;
    const Privacy privacy = classify_privacy(status, raw.vt,
        last_privacy_.boolean_observed ? last_privacy_.boolean : static_cast<VARIANT_BOOL>(1));
    if (trace_) trace_(trace_context_, stage_, GuardEvent::PrivacyObserved,
        GuardReason::None, status, &last_privacy_);
    const HRESULT cleared = VariantClear(&raw);  // Mandatory owned cleanup only.
    if (cleared != S_OK) return refuse(GuardReason::CleanupUnknown, cleared);
    if (GetTickCount64() >= deadline_) return refuse(GuardReason::Deadline);
    if (privacy == Privacy::Sensitive) return refuse(GuardReason::Sensitive, status);
    if (privacy != Privacy::NonSensitive) return refuse(GuardReason::PrivacyUnknown, status);
    return true;
}
bool Checkpoint::runtime(ObservedScope& observed) noexcept {
    UIA_HWND actual_hwnd = nullptr;
    int actual_pid = 0;
    HRESULT status = element_->get_CurrentNativeWindowHandle(&actual_hwnd);
    if (status != S_OK || actual_hwnd != observed.hwnd)
        return refuse(GuardReason::OwnerMismatch, status);
    status = element_->get_CurrentProcessId(&actual_pid);
    if (status != S_OK || actual_pid <= 0 || static_cast<DWORD>(actual_pid) != observed.pid)
        return refuse(GuardReason::OwnerMismatch, status);
    SAFEARRAY* array = nullptr;
    status = element_->GetRuntimeId(&array);
    if (status != S_OK || array == nullptr) {
        if (array) SafeArrayDestroy(array);
        return refuse(GuardReason::RuntimeUnavailable, status);
    }
    VARTYPE type = VT_EMPTY; LONG first = -1, last = -1;
    bool valid = SafeArrayGetDim(array) == 1 && SafeArrayGetVartype(array, &type) == S_OK &&
        type == VT_I4 && SafeArrayGetLBound(array, 1, &first) == S_OK &&
        SafeArrayGetUBound(array, 1, &last) == S_OK && first == 0 && last >= 0 && last < 64;
    if (valid) {
        observed.runtime.size = static_cast<std::size_t>(last + 1);
        for (LONG i = first; i <= last; ++i) {
            if (SafeArrayGetElement(array, &i, &observed.runtime.values[static_cast<std::size_t>(i)]) != S_OK) {
                valid = false; break;
            }
        }
    }
    const HRESULT destroyed = SafeArrayDestroy(array);
    if (!valid || destroyed != S_OK) return refuse(GuardReason::RuntimeUnavailable, status);
    return true;
}
bool Checkpoint::bind(IUIAutomation* automation, ExpectedOwner expected,
    GenerationReader generation, void* generation_context, GuardTrace trace, void* trace_context) noexcept {
    if (element_ || process_ || !automation || !generation || !expected.issued_hwnd || !expected.pid)
        return refuse(GuardReason::OwnerUnavailable);
    expected_ = expected; generation_ = generation; generation_context_ = generation_context;
    trace_ = trace; trace_context_ = trace_context; deadline_ = GetTickCount64() + 5000;
    if (!DuplicateHandle(GetCurrentProcess(), GetCurrentProcess(), GetCurrentProcess(), &process_,
        PROCESS_QUERY_INFORMATION | SYNCHRONIZE, FALSE, 0))
        return refuse(GuardReason::OwnerUnavailable);
    ObservedScope observed;
    if (!native_owner(observed)) return false;
    // Known issued HWND only; never desktop/root search or recovery re-find.
    HRESULT status = automation->ElementFromHandle(observed.hwnd, &element_);
    if (status != S_OK || element_ == nullptr) return refuse(GuardReason::ElementUnavailable, status);
    if (!nonsensitive() || !runtime(observed)) return false;
    baseline_ = observed;  // Actual native observations, never substituted expectations.
    if (!nonsensitive()) return false; // LAST provider operation before first probe.
    ObservedScope final_owner;
    if (!native_owner(final_owner)) return false;
    final_owner.runtime = observed.runtime;
    return final_owner == baseline_ || refuse(GuardReason::ScopeChanged);
}
bool Checkpoint::check() noexcept {
    ObservedScope observed;
    if (!native_owner(observed) || !nonsensitive() || !runtime(observed)) return false;
    if (!(observed == baseline_)) return refuse(GuardReason::ScopeChanged);
    if (!nonsensitive()) return false; // Metadata may have changed sensitivity.
    ObservedScope final_owner;
    if (!native_owner(final_owner)) return false;
    final_owner.runtime = observed.runtime;
    return final_owner == baseline_ || refuse(GuardReason::ScopeChanged);
}
openadapt::capability_prototype::Scope Checkpoint::sdk_scope() const noexcept {
    openadapt::capability_prototype::Scope scope{};
    // Fixture token only; complete native/runtime tuple is compared by check().
    const std::uint64_t hwnd = reinterpret_cast<std::uintptr_t>(baseline_.hwnd);
    std::memcpy(scope.identity, &hwnd, sizeof(hwnd));
    std::memcpy(scope.identity + 8, &baseline_.pid, sizeof(baseline_.pid));
    std::memcpy(scope.identity + 12, &baseline_.session, sizeof(baseline_.session));
    std::memcpy(scope.identity + 16, &baseline_.creation, sizeof(baseline_.creation));
    std::memcpy(scope.identity + 24, &baseline_.fixture_generation, sizeof(baseline_.fixture_generation));
    scope.generation = baseline_.fixture_generation;
    return scope;
}
bool __cdecl Checkpoint::callback(void* raw,
    const openadapt::capability_prototype::Scope& expected,
    openadapt::capability_prototype::Stage stage) noexcept {
    if (!raw) return false;
    auto& checkpoint = *static_cast<Checkpoint*>(raw);
    checkpoint.stage_ = static_cast<unsigned>(stage);
    if (checkpoint.trace_) checkpoint.trace_(checkpoint.trace_context_, checkpoint.stage_,
        GuardEvent::Enter, GuardReason::None, S_OK, nullptr);
    const auto actual = checkpoint.sdk_scope();
    if (expected.generation != actual.generation ||
        std::memcmp(expected.identity, actual.identity, sizeof(actual.identity)) != 0)
        return checkpoint.refuse(GuardReason::ScopeChanged);
    const bool accepted = checkpoint.check();
    if (accepted && checkpoint.trace_) checkpoint.trace_(checkpoint.trace_context_,
        checkpoint.stage_, GuardEvent::Accepted, GuardReason::None, S_OK, nullptr);
    return accepted;
}
}  // namespace openadapt::provider_source
