// Owned in-process COM objects only. No UIA activation, registration or getter.
#include "sdk_capability.h"
#include <OleAuto.h>
#include <array>
#include <cstdint>
#include <iostream>
#include <vector>

using namespace openadapt::capability_prototype;

struct Counts {
    ULONG refs = 1;
    unsigned adds = 0, releases = 0, destroyed = 0, qi = 0, unexpected = 0;
};
struct ErrorCounts : Counts {
    unsigned source = 0, description = 0, help_file = 0, guid = 0, help_context = 0;
};

class ErrorTrap final : public IErrorInfo {
    ErrorCounts& counts;
public:
    explicit ErrorTrap(ErrorCounts& c) : counts(c) {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        ++counts.qi;
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid != __uuidof(IUnknown) && iid != __uuidof(IErrorInfo)) return E_NOINTERFACE;
        *out = static_cast<IErrorInfo*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { ++counts.adds; return ++counts.refs; }
    ULONG STDMETHODCALLTYPE Release() override {
        ++counts.releases; const auto left = --counts.refs;
        if (!left) { ++counts.destroyed; delete this; }
        return left;
    }
    HRESULT STDMETHODCALLTYPE GetGUID(GUID* out) override {
        ++counts.guid; if (out) *out = GUID{}; return E_UNEXPECTED;
    }
    HRESULT STDMETHODCALLTYPE GetSource(BSTR* out) override {
        ++counts.source; if (out) *out = nullptr; return E_UNEXPECTED;
    }
    HRESULT STDMETHODCALLTYPE GetDescription(BSTR* out) override {
        ++counts.description; if (out) *out = nullptr; return E_UNEXPECTED;
    }
    HRESULT STDMETHODCALLTYPE GetHelpFile(BSTR* out) override {
        ++counts.help_file; if (out) *out = nullptr; return E_UNEXPECTED;
    }
    HRESULT STDMETHODCALLTYPE GetHelpContext(DWORD* out) override {
        ++counts.help_context; if (out) *out = 0; return E_UNEXPECTED;
    }
};

struct Mode {
    const char* name;
    HRESULT acquisition = S_OK;
    bool acquisition_output = true;
    HRESULT query = S_OK;
    bool query_output = true;
    int refuse_stage = -1;
    bool invalid_abi = false, null_element = false, null_callback = false;
    Reason expected_reason = Reason::Supported;
};

class Patterns final : public IUIAutomationTogglePattern,
    public IUIAutomationSelectionItemPattern, public IUIAutomationValuePattern {
    Counts& counts;
    const Mode& mode;
    IErrorInfo* trap;  // Borrowed; fixture holds its own reference throughout.
public:
    unsigned fault_error_sets = 0;
    HRESULT fault_set_hresult = S_OK;
    bool same_interface_alias = false;
    void* acquired_view = nullptr;  // Internal comparison only; never serialized.
    Patterns(Counts& c, const Mode& m, IErrorInfo* t) : counts(c), mode(m), trap(t) {}
    void* view(REFIID iid) noexcept {
        if (iid == __uuidof(IUIAutomationTogglePattern)) return static_cast<IUIAutomationTogglePattern*>(this);
        if (iid == __uuidof(IUIAutomationSelectionItemPattern)) return static_cast<IUIAutomationSelectionItemPattern*>(this);
        if (iid == __uuidof(IUIAutomationValuePattern)) return static_cast<IUIAutomationValuePattern*>(this);
        if (iid == __uuidof(IUnknown)) return static_cast<IUIAutomationTogglePattern*>(this);
        return nullptr;
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        ++counts.qi;
        if (!out) return E_POINTER;
        *out = nullptr;
        void* typed = view(iid);
        if (!typed) return E_NOINTERFACE;
        if (mode.query_output) {
            *out = typed; AddRef(); same_interface_alias = typed == acquired_view;
        }
        if (mode.query != S_OK) { fault_set_hresult = SetErrorInfo(0, trap); ++fault_error_sets; }
        return mode.query;  // Owned failure injection; output still a valid SDK view.
    }
    ULONG STDMETHODCALLTYPE AddRef() override { ++counts.adds; return ++counts.refs; }
    ULONG STDMETHODCALLTYPE Release() override {
        ++counts.releases; const auto left = --counts.refs;
        if (!left) { ++counts.destroyed; delete this; }
        return left;
    }
    HRESULT unexpected() noexcept { ++counts.unexpected; return E_NOTIMPL; }
#include "fixture_patterns_stubs.inc"
};

class Element final : public IUIAutomationElement {
    Counts& counts;
    const Mode& mode;
    Patterns& patterns;
    IErrorInfo* trap;
    PATTERNID expected_id;
    const IID& expected_iid;
public:
    unsigned acquisitions = 0, mismatched_request = 0, fault_error_sets = 0;
    HRESULT fault_set_hresult = S_OK;
    Element(Counts& c, const Mode& m, Patterns& p, IErrorInfo* t, PATTERNID id, const IID& iid)
        : counts(c), mode(m), patterns(p), trap(t), expected_id(id), expected_iid(iid) {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        ++counts.qi;
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid != __uuidof(IUnknown) && iid != __uuidof(IUIAutomationElement)) return E_NOINTERFACE;
        *out = static_cast<IUIAutomationElement*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { ++counts.adds; return ++counts.refs; }
    ULONG STDMETHODCALLTYPE Release() override {
        ++counts.releases; const auto left = --counts.refs;
        if (!left) { ++counts.destroyed; delete this; }
        return left;
    }
    HRESULT STDMETHODCALLTYPE GetCurrentPatternAs(PATTERNID id, REFIID iid, void** out) override {
        ++acquisitions;
        if (!out) return E_POINTER;
        *out = nullptr;
        if (id != expected_id || iid != expected_iid) { ++mismatched_request; return E_NOINTERFACE; }
        if (mode.acquisition_output) {
            *out = patterns.view(iid);
            if (!*out) return E_NOINTERFACE;
            patterns.AddRef(); patterns.acquired_view = *out;
        }
        if (mode.acquisition != S_OK) { fault_set_hresult = SetErrorInfo(0, trap); ++fault_error_sets; }
        return mode.acquisition;
    }
    HRESULT unexpected() noexcept { ++counts.unexpected; return E_NOTIMPL; }
#include "fixture_element_stubs.inc"
};

struct Gate {
    Scope expected{};
    int refuse = -1;
    unsigned count = 0;
    std::array<Stage, 8> visits{};
    bool overflow = false;
};
static bool __cdecl checkpoint(void* raw, const Scope& expected, Stage stage) noexcept {
    auto& gate = *static_cast<Gate*>(raw);
    if (gate.count >= gate.visits.size()) { gate.overflow = true; return false; }
    gate.visits[gate.count++] = stage;
    for (unsigned i = 0; i < 32; ++i) if (expected.identity[i] != gate.expected.identity[i]) return false;
    if (expected.generation != gate.expected.generation) return false;
    return static_cast<int>(stage) != gate.refuse;
}
static bool same_stages(const Gate& gate, const std::vector<Stage>& expected) {
    if (gate.overflow || gate.count != expected.size()) return false;
    for (unsigned i = 0; i < gate.count; ++i) if (gate.visits[i] != expected[i]) return false;
    return true;
}
static const char* failure = nullptr;
static bool check(bool condition, const char* label) {
    if (!condition && !failure) failure = label;
    return condition;
}

static bool run_case(Pattern pattern, PATTERNID id, REFIID iid, const Mode& mode) {
    Counts pc, ec;
    ErrorCounts tc;
    auto* trap = new ErrorTrap(tc);
    auto* patterns = new Patterns(pc, mode, trap);
    auto* element = new Element(ec, mode, *patterns, trap, id, iid);
    Gate gate; gate.expected.identity[0] = 1; gate.expected.generation = 7; gate.refuse = mode.refuse_stage;
    const HRESULT setup_tls_hr = SetErrorInfo(0, trap);
    check(setup_tls_hr == S_OK, "trap_setup");
    const ErrorCounts trap_before = tc;
    // Subject begins AFTER setup. All owned objects stay live through callbacks.
    const Observation result = observe_capability(mode.null_element ? nullptr : element,
        pattern, mode.invalid_abi ? kSourceAbi + 1 : kSourceAbi, gate.expected,
        mode.null_callback ? nullptr : checkpoint, &gate);
    const ErrorCounts trap_after = tc;
    check(result.reason == mode.expected_reason, "closed_reason");
    const bool preflight = mode.invalid_abi || mode.null_element || mode.null_callback || static_cast<unsigned>(pattern) > 2;
    const bool acquire = !preflight && mode.refuse_stage != static_cast<int>(Stage::BeforeAcquire);
    const bool query = acquire && mode.acquisition == S_OK && mode.acquisition_output &&
        mode.refuse_stage != static_cast<int>(Stage::BeforeQueryInterface);
    const bool supported = query && mode.query == S_OK && mode.query_output && mode.refuse_stage == -1;
    const bool unsupported = acquire && mode.acquisition == UIA_E_NOTSUPPORTED && !mode.acquisition_output &&
        mode.refuse_stage != static_cast<int>(Stage::AfterUnsupported);
    check(result.capability == (supported ? Capability::Supported : unsupported ? Capability::ExplicitlyUnsupported : Capability::Unknown), "capability");
    check(result.acquire_attempted == acquire && element->acquisitions == (acquire ? 1u : 0u), "acquire_attempted");
    check(result.query_attempted == query && pc.qi == (query ? 1u : 0u), "query_attempted");
    if (acquire) check(result.acquire_hresult == mode.acquisition, "signed_acquire_hresult");
    if (query) check(result.query_hresult == mode.query, "signed_query_hresult");
    check(result.acquire_nonnull == (acquire && mode.acquisition_output), "acquire_output");
    check(result.query_nonnull == (query && mode.query_output), "query_output");
    const unsigned owned = (acquire && mode.acquisition_output ? 1u : 0u) + (query && mode.query_output ? 1u : 0u);
    const unsigned subject_pattern_releases = pc.releases;
    const bool observed_alias = patterns->same_interface_alias;
    check(pc.refs == 1 && pc.adds == owned && pc.releases == owned && pc.destroyed == 0, "owned_pattern_references");
    check(ec.refs == 1 && ec.adds == 0 && ec.releases == 0 && ec.destroyed == 0, "borrowed_element_lifetime");
    check(!owned || !query || !mode.query_output || patterns->same_interface_alias, "same_interface_alias");
    check(element->mismatched_request == 0 && pc.unexpected == 0 && ec.unexpected == 0, "no_semantic_or_wrong_iid_calls");
    std::vector<Stage> stages;
    if (!preflight) stages.push_back(Stage::BeforeAcquire);
    if (acquire && mode.acquisition == UIA_E_NOTSUPPORTED && !mode.acquisition_output) stages.push_back(Stage::AfterUnsupported);
    if (acquire && mode.acquisition == S_OK && mode.acquisition_output) {
        stages.push_back(Stage::BeforeQueryInterface);
        if (query && mode.query == S_OK && mode.query_output) {
            stages.push_back(Stage::AfterQueryInterface);
            if (mode.refuse_stage != static_cast<int>(Stage::AfterQueryInterface)) stages.push_back(Stage::AfterRelease);
        }
    }
    check(same_stages(gate, stages), "checkpoint_order");
    check(trap_after.description == trap_before.description && trap_after.source == trap_before.source &&
        trap_after.help_file == trap_before.help_file && trap_after.guid == trap_before.guid &&
        trap_after.help_context == trap_before.help_context, "no_error_info_getters");
    check(element->fault_set_hresult == S_OK && patterns->fault_set_hresult == S_OK, "fault_set_error_info_hresult");
    // Subject ends BEFORE explicit TLS inspection/cleanup. GetErrorInfo consumes
    // the TLS-owned ref; it does not call the trap's string accessors here.
    IErrorInfo* returned = nullptr;
    const HRESULT tls_hr = GetErrorInfo(0, &returned);
    const bool survived = tls_hr == S_OK && returned == static_cast<IErrorInfo*>(trap);
    check(survived, "tls_trap_survival");
    if (returned) returned->Release();
    IErrorInfo* cleared = nullptr;
    check(GetErrorInfo(0, &cleared) == S_FALSE && cleared == nullptr, "tls_consumed_once");
    if (cleared) cleared->Release();
    const HRESULT cleanup_tls_hr = SetErrorInfo(0, nullptr);
    check(cleanup_tls_hr == S_OK, "tls_cleanup");
    const unsigned operation_sets = element->fault_error_sets + patterns->fault_error_sets;
    const unsigned element_sets = element->fault_error_sets, query_sets = patterns->fault_error_sets;
    const HRESULT element_set_hr = element->fault_set_hresult, query_set_hr = patterns->fault_set_hresult;
    element->Release(); patterns->Release(); trap->Release();
    check(ec.destroyed == 1 && pc.destroyed == 1 && tc.destroyed == 1 && ec.refs == 0 && pc.refs == 0 && tc.refs == 0, "final_owned_cleanup");
    std::cout << "{\"case\":\"" << mode.name << "\",\"pattern\":" << static_cast<unsigned>(pattern)
        << ",\"capability\":" << static_cast<unsigned>(result.capability)
        << ",\"reason\":" << static_cast<unsigned>(result.reason)
        << ",\"refused_stage\":" << mode.refuse_stage
        << ",\"acquire_attempted\":" << result.acquire_attempted << ",\"acquire_hr\":" << result.acquire_hresult
        << ",\"query_attempted\":" << result.query_attempted << ",\"query_hr\":" << result.query_hresult
        << ",\"pattern_refs_added\":" << pc.adds << ",\"pattern_refs_released_subject\":" << subject_pattern_releases
        << ",\"expected_owned_pattern_refs\":" << owned << ",\"observed_alias\":" << observed_alias
        << ",\"pattern_refs_released_total\":" << pc.releases << ",\"checkpoint_count\":" << gate.count
        << ",\"stages\":[";
    for (unsigned i = 0; i < gate.count; ++i) { if (i) std::cout << ','; std::cout << static_cast<unsigned>(gate.visits[i]); }
    std::cout << "]"
        << ",\"setup_tls_hr\":" << static_cast<std::int32_t>(setup_tls_hr)
        << ",\"cleanup_tls_hr\":" << static_cast<std::int32_t>(cleanup_tls_hr)
        << ",\"element_fault_sets\":" << element_sets << ",\"element_fault_set_hr\":" << static_cast<std::int32_t>(element_set_hr)
        << ",\"query_fault_sets\":" << query_sets << ",\"query_fault_set_hr\":" << static_cast<std::int32_t>(query_set_hr)
        << ",\"fault_error_info_sets\":" << operation_sets << ",\"tls_survived\":" << survived
        << ",\"error_info_subject_getters\":{\"description\":" << trap_after.description - trap_before.description
        << ",\"source\":" << trap_after.source - trap_before.source
        << ",\"help_file\":" << trap_after.help_file - trap_before.help_file
        << ",\"guid\":" << trap_after.guid - trap_before.guid
        << ",\"help_context\":" << trap_after.help_context - trap_before.help_context << "}"
        << ",\"error_info_setup_getters\":" << (trap_before.description + trap_before.source + trap_before.help_file + trap_before.guid + trap_before.help_context)
        << ",\"error_info_cleanup_getters\":" << ((tc.description - trap_after.description) + (tc.source - trap_after.source) + (tc.help_file - trap_after.help_file) + (tc.guid - trap_after.guid) + (tc.help_context - trap_after.help_context))
        << ",\"unexpected_semantic_calls\":" << (ec.unexpected + pc.unexpected)
        << ",\"all_destroyed\":" << (ec.destroyed == 1 && pc.destroyed == 1 && tc.destroyed == 1)
        << ",\"passed\":" << (failure == nullptr) << "}" << std::endl;
    return failure == nullptr;
}

static bool owned_oracle_controls() {
    ErrorCounts errors;
    auto* trap = new ErrorTrap(errors);
    BSTR output = nullptr; GUID guid{}; DWORD help = 1;
    check(trap->GetDescription(&output) == E_UNEXPECTED && trap->GetSource(&output) == E_UNEXPECTED &&
        trap->GetHelpFile(&output) == E_UNEXPECTED && trap->GetGUID(&guid) == E_UNEXPECTED &&
        trap->GetHelpContext(&help) == E_UNEXPECTED, "trap_oracle_hresult");
    check(errors.description == 1 && errors.source == 1 && errors.help_file == 1 &&
        errors.guid == 1 && errors.help_context == 1 && output == nullptr && help == 0, "trap_positive_oracle");
    trap->Release();
    check(errors.destroyed == 1, "oracle_cleanup");
    std::cout << "{\"oracle_controls\":1,\"owned_error_getters_live\":1,\"passed\":" << (failure == nullptr) << "}" << std::endl;
    return failure == nullptr;
}

int main() {
    std::cout << std::boolalpha;
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (initialized != S_OK && initialized != S_FALSE) return 3;
    bool good = owned_oracle_controls();
    const std::array<Mode, 13> modes{{
        {"supported"}, {"explicit_unsupported", UIA_E_NOTSUPPORTED, false},
        {"normal_null", S_OK, false}, {"s_false_null", S_FALSE, false},
        {"s_false_output", S_FALSE, true}, {"failure_null", E_FAIL, false},
        {"failure_output", E_FAIL, true}, {"unsupported_output", UIA_E_NOTSUPPORTED, true},
        {"qi_no_interface", S_OK, true, E_NOINTERFACE, false},
        {"qi_failure_output", S_OK, true, E_FAIL, true},
        {"qi_normal_null", S_OK, true, S_OK, false},
        {"qi_s_false_output", S_OK, true, S_FALSE, true},
        {"qi_not_supported", S_OK, true, UIA_E_NOTSUPPORTED, false}
    }};
    const std::array<Reason, 13> expected_reasons{{Reason::Supported, Reason::ExplicitlyUnsupported,
        Reason::NormalNull, Reason::Acquisition, Reason::Acquisition, Reason::Acquisition,
        Reason::Acquisition, Reason::ContradictoryOutput, Reason::QueryInterface,
        Reason::QueryInterface, Reason::QueryInterface, Reason::QueryInterface, Reason::QueryInterface}};
    unsigned cases = 0;
    for (unsigned index = 0; index < 3 && good; ++index) {
        const Pattern pattern = static_cast<Pattern>(index);
        const PATTERNID id = index == 0 ? UIA_TogglePatternId : index == 1 ? UIA_SelectionItemPatternId : UIA_ValuePatternId;
        const IID& iid = index == 0 ? __uuidof(IUIAutomationTogglePattern) : index == 1 ? __uuidof(IUIAutomationSelectionItemPattern) : __uuidof(IUIAutomationValuePattern);
        for (unsigned m = 0; m < modes.size(); ++m) { Mode mode = modes[m]; mode.expected_reason = expected_reasons[m]; good = run_case(pattern, id, iid, mode); ++cases; if (!good) break; }
        for (int stage = 0; stage < 4 && good; ++stage) {
            Mode mode{"guard_refused"}; mode.refuse_stage = stage; mode.expected_reason = Reason::GuardOrScope;
            good = run_case(pattern, id, iid, mode); ++cases;
        }
        if (good) { Mode mode{"unsupported_guard_refused", UIA_E_NOTSUPPORTED, false}; mode.refuse_stage = 4; mode.expected_reason = Reason::GuardOrScope; good = run_case(pattern,id,iid,mode); ++cases; }
        if (good) { Mode mode{"invalid_abi"}; mode.invalid_abi = true; mode.expected_reason = Reason::InvalidRequest; good = run_case(pattern,id,iid,mode); ++cases; }
        if (good) { Mode mode{"null_element"}; mode.null_element = true; mode.expected_reason = Reason::InvalidRequest; good = run_case(pattern,id,iid,mode); ++cases; }
        if (good) { Mode mode{"null_callback"}; mode.null_callback = true; mode.expected_reason = Reason::InvalidRequest; good = run_case(pattern,id,iid,mode); ++cases; }
    }
    if (good) { Mode mode{"invalid_pattern"}; mode.expected_reason = Reason::InvalidRequest; good = run_case(static_cast<Pattern>(99), UIA_ValuePatternId, __uuidof(IUIAutomationValuePattern), mode); ++cases; }
    SetErrorInfo(0, nullptr); CoUninitialize();
    std::cout << "{\"summary\":1,\"cases\":" << cases << ",\"passed\":" << good
        << ",\"fixed_failure\":\"" << (failure ? failure : "none") << "\",\"live_uia_provider_used\":false}" << std::endl;
    return good && cases == 64 ? 0 : 2;
}
