// Handwritten mechanism using SDK declarations; no vendored SDK implementation.
#include "sdk_capability.h"

namespace openadapt::capability_prototype {
static_assert(sizeof(HRESULT) == sizeof(std::int32_t));
static_assert(static_cast<HRESULT>(-1) < 0);

namespace {
template<class Interface> class Owned {
public:
    Interface* pointer = nullptr;  // Caller-owned, zero-initialized out storage.
    Owned() = default;
    Owned(const Owned&) = delete;
    Owned& operator=(const Owned&) = delete;
    ~Owned() noexcept { reset(); }
    void reset() noexcept {
        Interface* previous = pointer;
        pointer = nullptr;
        if (previous != nullptr) previous->Release();
    }
};

template<class Interface>
Observation acquire(
    IUIAutomationElement* element, PATTERNID pattern_id, const Scope& scope,
    Checkpoint checkpoint, void* context) noexcept {
    Observation result;
    Owned<Interface> acquired;
    Owned<Interface> verified;
    if (!checkpoint(context, scope, Stage::BeforeAcquire)) {
        result.reason = Reason::GuardOrScope;
        return result;
    }
    // Direct SDK-typed HRESULT call: no _com_error, exception marshaler,
    // GetErrorInfo, IErrorInfo methods, wrapper truthiness or fallback.
    const HRESULT hr = element->GetCurrentPatternAs(
        pattern_id, IID_PPV_ARGS(&acquired.pointer));
    result.acquire_attempted = true;
    result.acquire_hresult = static_cast<std::int32_t>(hr);
    result.acquire_nonnull = acquired.pointer != nullptr;
    if (hr == UIA_E_NOTSUPPORTED) {
        if (acquired.pointer != nullptr) {
            result.reason = Reason::ContradictoryOutput;
            return result;
        }
        if (!checkpoint(context, scope, Stage::AfterUnsupported)) {
            result.reason = Reason::GuardOrScope;
            return result;
        }
        result.capability = Capability::ExplicitlyUnsupported;
        result.reason = Reason::ExplicitlyUnsupported;
        return result;
    }
    if (hr != S_OK) {
        result.reason = Reason::Acquisition;
        return result;
    }
    if (acquired.pointer == nullptr) {
        result.reason = Reason::NormalNull;
        return result;
    }
    if (!checkpoint(context, scope, Stage::BeforeQueryInterface)) {
        result.reason = Reason::GuardOrScope;
        return result;
    }
    // IID_PPV_ARGS binds both calls to the same exact SDK Interface type.
    const HRESULT qi = acquired.pointer->QueryInterface(IID_PPV_ARGS(&verified.pointer));
    result.query_attempted = true;
    result.query_hresult = static_cast<std::int32_t>(qi);
    result.query_nonnull = verified.pointer != nullptr;
    if (qi != S_OK || verified.pointer == nullptr) {
        result.reason = Reason::QueryInterface;
        return result;
    }
    if (!checkpoint(context, scope, Stage::AfterQueryInterface)) {
        result.reason = Reason::GuardOrScope;
        return result;
    }
    // Mandatory ownership cleanup is permitted after refusal. Positive results
    // get a final fresh checkpoint after cleanup; no diagnostic getter is added.
    verified.reset();
    acquired.reset();
    if (!checkpoint(context, scope, Stage::AfterRelease)) {
        result.reason = Reason::GuardOrScope;
        return result;
    }
    result.capability = Capability::Supported;
    result.reason = Reason::Supported;
    return result;
}
}  // namespace

Observation observe_capability(
    IUIAutomationElement* borrowed_element, Pattern pattern,
    std::uint32_t source_abi, Scope expected_scope,
    Checkpoint checkpoint, void* checkpoint_context) noexcept {
    if (source_abi != kSourceAbi || borrowed_element == nullptr || checkpoint == nullptr)
        return {};
    switch (pattern) {
    case Pattern::Toggle:
        return acquire<IUIAutomationTogglePattern>(borrowed_element, UIA_TogglePatternId,
            expected_scope, checkpoint, checkpoint_context);
    case Pattern::SelectionItem:
        return acquire<IUIAutomationSelectionItemPattern>(borrowed_element,
            UIA_SelectionItemPatternId, expected_scope, checkpoint, checkpoint_context);
    case Pattern::Value:
        return acquire<IUIAutomationValuePattern>(borrowed_element, UIA_ValuePatternId,
            expected_scope, checkpoint, checkpoint_context);
    }
    return {};
}
}  // namespace openadapt::capability_prototype
