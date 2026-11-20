// SOURCE ONLY. An instrumented OWNED provider, not a runtime/agent backend.
#include "sdk_checkpoint.h"
#include <OleAuto.h>
#include <atomic>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>

using namespace openadapt::provider_source;
namespace cap = openadapt::capability_prototype;
namespace {
enum class PrivacyMode { False, True, ReservedUnsupported, WrongKind, Failure, EmptyFallback };
enum class PatternMode { Supported, NormalNull, ExplicitUnsupported, Failure };
enum class Case { Supported, NormalNull, ExplicitUnsupported, PatternError, Sensitive,
    UnsupportedPrivacy, WrongPrivacyKind, PrivacyError, EmptyFallback, WrongSession,
    WrongGeneration, Expired, SensitiveBeforeAcquire, SensitiveBeforeQI, SensitiveAfterQI,
    DestroyedBeforeAcquire };
enum class Event { WindowCreate, WindowDestroy, ProviderOptions, HostProvider,
    Property, Pattern, QueryInterface, AddRef, Release, Semantic, Action,
    Guard, ErrorGetter, WindowText, UnknownInterface, PrivacyObserved, PropertyOutput,
    ErrorSet, OracleGetter, PatternOutput, WindowObject, PumpExit, WindowCleanup, WindowFinalDestroy };
struct Entry { Event event; LONG argument; HRESULT status; unsigned phase; DWORD thread; std::uint64_t generation; VARTYPE kind; bool boolean_observed; VARIANT_BOOL boolean; };
struct State {
    std::mutex mutex;
    std::vector<Entry> entries;
    std::array<std::uint64_t, 24> event_counts{};
    bool overflow = false;
    std::atomic<HWND> hwnd{nullptr};
    std::atomic<std::uint64_t> generation{0};
    std::atomic<unsigned> phase{5};
    std::atomic<unsigned> forbidden{0}, unknown{0}, live_objects{0};
    std::atomic<PrivacyMode> privacy{PrivacyMode::False};
    Case selected = Case::Supported;
    std::atomic<bool> stop{false}, window_final_destroyed{false}, class_unregistered{false},
        cleanup_failed{false}, ui_completed{false};
    std::atomic<DWORD> ui_thread{0};
    PatternMode pattern = PatternMode::Supported;
    HANDLE ready = nullptr, destroyed = nullptr;
    ObservedScope issued_scope{};
    void add(Event event, LONG argument = 0, HRESULT status = S_OK, const PrivacyObservation* raw = nullptr) noexcept {
        std::lock_guard<std::mutex> lock(mutex);
        ++event_counts[static_cast<unsigned>(event)];
        if (entries.size() >= 1024) { overflow = true; return; }
        entries.push_back({event, argument, status, phase.load(), GetCurrentThreadId(), generation.load(),
            static_cast<VARTYPE>(raw ? raw->kind : VT_EMPTY), raw && raw->boolean_observed,
            raw ? raw->boolean : static_cast<VARIANT_BOOL>(0)});
    }
    HRESULT semantic(LONG id, bool action = false) noexcept {
        ++forbidden; add(action ? Event::Action : Event::Semantic, id, E_ACCESSDENIED);
        return E_ACCESSDENIED;
    }
};
std::uint64_t generation(void* raw, HWND hwnd) noexcept {
    auto& state = *static_cast<State*>(raw);
    return state.hwnd.load() == hwnd ? state.generation.load() : 0;
}
void guard_trace(void* raw, unsigned phase, GuardEvent event, GuardReason reason,
    HRESULT status, const PrivacyObservation* raw_privacy) noexcept {
    auto& state = *static_cast<State*>(raw); state.phase = phase;
    state.add(raw_privacy ? Event::PrivacyObserved : Event::Guard,
        static_cast<LONG>(event) * 100 + static_cast<LONG>(reason), status, raw_privacy);
    if (event != GuardEvent::Enter) return;
    if ((state.selected == Case::SensitiveBeforeAcquire && phase == 0) ||
        (state.selected == Case::SensitiveBeforeQI && phase == 1) ||
        (state.selected == Case::SensitiveAfterQI && phase == 2)) state.privacy = PrivacyMode::True;
    if (state.selected == Case::DestroyedBeforeAcquire && phase == 0) {
        if (!PostMessageW(state.hwnd.load(), WM_CLOSE, 0, 0) ||
            WaitForSingleObject(state.destroyed, 2000) != WAIT_OBJECT_0) ++state.unknown;
    }
}

class ErrorTrap final : public IErrorInfo {
    std::atomic<ULONG> refs_{1}; State& state_; bool oracle_;
public:
    explicit ErrorTrap(State& state, bool oracle = false) : state_(state), oracle_(oracle) { ++state_.live_objects; }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER; *out = nullptr;
        if (iid != __uuidof(IUnknown) && iid != __uuidof(IErrorInfo)) {
            ++state_.unknown; state_.add(Event::UnknownInterface, 4); return E_NOINTERFACE;
        }
        state_.add(Event::QueryInterface, iid == __uuidof(IUnknown) ? 4 : 5);
        *out = static_cast<IErrorInfo*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { state_.add(Event::AddRef, 4); return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG count = --refs_; state_.add(Event::Release, 4);
        if (!count) { --state_.live_objects; delete this; } return count;
    }
    HRESULT get(LONG id) noexcept { if (!oracle_) ++state_.forbidden;
        state_.add(oracle_ ? Event::OracleGetter : Event::ErrorGetter, id, E_UNEXPECTED); return E_UNEXPECTED; }
    HRESULT STDMETHODCALLTYPE GetGUID(GUID* out) override { if (out) *out = GUID{}; return get(0); }
    HRESULT STDMETHODCALLTYPE GetSource(BSTR* out) override { if (out) *out = nullptr; return get(1); }
    HRESULT STDMETHODCALLTYPE GetDescription(BSTR* out) override { if (out) *out = nullptr; return get(2); }
    HRESULT STDMETHODCALLTYPE GetHelpFile(BSTR* out) override { if (out) *out = nullptr; return get(3); }
    HRESULT STDMETHODCALLTYPE GetHelpContext(DWORD* out) override { if (out) *out = 0; return get(4); }
};

class Provider final : public IRawElementProviderSimple, public IToggleProvider,
    public ISelectionItemProvider, public IValueProvider {
    std::atomic<ULONG> refs_{1}; State& state_; IErrorInfo* trap_;
    HRESULT fail(HRESULT status) noexcept {
        const HRESULT set = SetErrorInfo(0, trap_);
        state_.add(Event::ErrorSet, 0, set);
        if (set != S_OK) { ++state_.unknown; state_.add(Event::ErrorGetter, -1, set); }
        return status;
    }
    HRESULT unsupported(VARIANT* value) noexcept {
        value->vt = VT_UNKNOWN;
        const HRESULT status = UiaGetReservedNotSupportedValue(&value->punkVal);
        if (status != S_OK) { value->vt = VT_EMPTY; ++state_.unknown; }
        return status;
    }
public:
    explicit Provider(State& state) : state_(state), trap_(new ErrorTrap(state)) { ++state_.live_objects; }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER; *out = nullptr;
        LONG category = 0;
        if (iid == __uuidof(IUnknown) || iid == __uuidof(IRawElementProviderSimple))
            *out = static_cast<IRawElementProviderSimple*>(this);
        else if (iid == __uuidof(IToggleProvider)) { category = 1; *out = static_cast<IToggleProvider*>(this); }
        else if (iid == __uuidof(ISelectionItemProvider)) { category = 2; *out = static_cast<ISelectionItemProvider*>(this); }
        else if (iid == __uuidof(IValueProvider)) { category = 3; *out = static_cast<IValueProvider*>(this); }
        else { ++state_.unknown; state_.add(Event::UnknownInterface); return E_NOINTERFACE; }
        state_.add(Event::QueryInterface, category); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { const ULONG count = ++refs_; state_.add(Event::AddRef); return count; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG count = --refs_; state_.add(Event::Release);
        if (!count) { trap_->Release(); --state_.live_objects; delete this; } return count;
    }
    HRESULT STDMETHODCALLTYPE get_ProviderOptions(ProviderOptions* out) override {
        state_.add(Event::ProviderOptions); if (!out) return E_POINTER;
        // No unimplemented COM-threading/focus/override claim. State is synchronized.
        *out = ProviderOptions_ServerSideProvider; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE get_HostRawElementProvider(IRawElementProviderSimple** out) override {
        state_.add(Event::HostProvider); if (!out) return E_POINTER; *out = nullptr;
        return UiaHostProviderFromHwnd(state_.hwnd.load(), out);
    }
    HRESULT pattern(PATTERNID id, IUnknown** out) {
        state_.add(Event::Pattern, id); if (!out) return E_POINTER; *out = nullptr;
        if (id == UIA_TextPatternId || id == UIA_LegacyIAccessiblePatternId) ++state_.forbidden;
        if (id != UIA_TogglePatternId && id != UIA_SelectionItemPatternId && id != UIA_ValuePatternId) {
            ++state_.unknown; return S_OK;
        }
        if (state_.pattern == PatternMode::NormalNull) return S_OK;
        if (state_.pattern == PatternMode::ExplicitUnsupported) return fail(UIA_E_NOTSUPPORTED);
        if (state_.pattern == PatternMode::Failure) return fail(E_FAIL);
        if (id == UIA_TogglePatternId) *out = static_cast<IToggleProvider*>(this);
        else if (id == UIA_SelectionItemPatternId) *out = static_cast<ISelectionItemProvider*>(this);
        else if (id == UIA_ValuePatternId) *out = static_cast<IValueProvider*>(this);
        else { ++state_.unknown; return S_OK; }
        AddRef(); return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetPatternProvider(PATTERNID id, IUnknown** out) override {
        const HRESULT status = pattern(id, out);
        state_.add(Event::PatternOutput, out && *out ? id : -id, status); return status;
    }
    HRESULT property(PROPERTYID id, VARIANT* out) {
        state_.add(Event::Property, id); if (!out) return E_POINTER; VariantInit(out);
        if (id == UIA_IsPasswordPropertyId) {
            switch (state_.privacy.load()) {
            case PrivacyMode::False: out->vt = VT_BOOL; out->boolVal = VARIANT_FALSE; return S_OK;
            case PrivacyMode::True: out->vt = VT_BOOL; out->boolVal = VARIANT_TRUE; return S_OK;
            case PrivacyMode::ReservedUnsupported: return unsupported(out);
            case PrivacyMode::WrongKind: out->vt = VT_I4; out->lVal = 0; return S_OK;
            case PrivacyMode::Failure: return fail(E_FAIL);
            case PrivacyMode::EmptyFallback: return S_OK;  // Deliberately distinct, NOT an Unknown oracle.
            }
        }
        if (id == UIA_ProcessIdPropertyId) { out->vt = VT_I4; out->lVal = static_cast<LONG>(GetCurrentProcessId()); return S_OK; }
        if (id == UIA_NativeWindowHandlePropertyId) { out->vt = VT_I4; out->lVal = HandleToLong(state_.hwnd.load()); return S_OK; }
        if (id == UIA_ControlTypePropertyId) { out->vt = VT_I4; out->lVal = UIA_CustomControlTypeId; return S_OK; }
        if (id == UIA_IsControlElementPropertyId || id == UIA_IsContentElementPropertyId) {
            out->vt = VT_BOOL; out->boolVal = VARIANT_TRUE; return S_OK;
        }
        if (id == UIA_NamePropertyId || id == UIA_ValueValuePropertyId) ++state_.forbidden;
        else ++state_.unknown;
        return unsupported(out);  // Prevent passthrough; never supply a caption/value.
    }
    HRESULT STDMETHODCALLTYPE GetPropertyValue(PROPERTYID id, VARIANT* out) override {
        const HRESULT status = property(id, out);
        PrivacyObservation raw{status, static_cast<VARTYPE>(out ? out->vt : VT_EMPTY), false, 0};
        if (out && status == S_OK && out->vt == VT_BOOL) {
            raw.boolean_observed = true; raw.boolean = out->boolVal;
        }
        state_.add(Event::PropertyOutput, id, status, &raw); return status;
    }
    HRESULT STDMETHODCALLTYPE Toggle() override { return state_.semantic(UIA_TogglePatternId, true); }
    HRESULT STDMETHODCALLTYPE get_ToggleState(ToggleState* out) override { if (out) *out = ToggleState_Off; return state_.semantic(UIA_TogglePatternId); }
    HRESULT STDMETHODCALLTYPE Select() override { return state_.semantic(UIA_SelectionItemPatternId, true); }
    HRESULT STDMETHODCALLTYPE AddToSelection() override { return state_.semantic(UIA_SelectionItemPatternId, true); }
    HRESULT STDMETHODCALLTYPE RemoveFromSelection() override { return state_.semantic(UIA_SelectionItemPatternId, true); }
    HRESULT STDMETHODCALLTYPE get_IsSelected(BOOL* out) override { if (out) *out = FALSE; return state_.semantic(UIA_SelectionItemPatternId); }
    HRESULT STDMETHODCALLTYPE get_SelectionContainer(IRawElementProviderSimple** out) override { if (out) *out = nullptr; return state_.semantic(UIA_SelectionItemPatternId); }
    HRESULT STDMETHODCALLTYPE SetValue(LPCWSTR) override { return state_.semantic(UIA_ValuePatternId, true); }
    HRESULT STDMETHODCALLTYPE get_Value(BSTR* out) override { if (out) *out = nullptr; return state_.semantic(UIA_ValuePatternId); }
    HRESULT STDMETHODCALLTYPE get_IsReadOnly(BOOL* out) override { if (out) *out = TRUE; return state_.semantic(UIA_ValuePatternId); }
};
struct WindowData { State* state; Provider* provider; };
LRESULT CALLBACK window_proc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    auto* data = reinterpret_cast<WindowData*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (message == WM_NCCREATE) {
        data = static_cast<WindowData*>(reinterpret_cast<CREATESTRUCTW*>(lparam)->lpCreateParams);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(data));
        data->state->hwnd = hwnd; ++data->state->generation;
        auto& issued = data->state->issued_scope;
        issued.hwnd = hwnd; issued.pid = GetCurrentProcessId(); issued.tid = GetCurrentThreadId();
        issued.fixture_generation = data->state->generation.load();
        FILETIME exit{}, kernel{}, user{};
        if (!GetProcessTimes(GetCurrentProcess(), &issued.creation, &exit, &kernel, &user) ||
            !ProcessIdToSessionId(issued.pid, &issued.session)) ++data->state->unknown;
        data->state->add(Event::WindowCreate);
    }
    if (data && message == WM_GETOBJECT) {
        data->state->add(Event::WindowObject, static_cast<LONG>(lparam));
        if (static_cast<DWORD>(lparam) == static_cast<DWORD>(UiaRootObjectId))
            return UiaReturnRawElementProvider(hwnd, wparam, lparam, data->provider);
        ++data->state->forbidden; data->state->add(Event::WindowText, -1);
        return 0;  // No Legacy/MSAA bridge.
    }
    if (data && (message == WM_GETTEXT || message == WM_GETTEXTLENGTH)) {
        ++data->state->forbidden; data->state->add(Event::WindowText, message);
        return 0;
    }
    if (data && message == WM_DESTROY) {
        UiaReturnRawElementProvider(hwnd, 0, 0, nullptr);
        data->state->hwnd = nullptr; ++data->state->generation;
        data->state->add(Event::WindowDestroy); SetEvent(data->state->destroyed); PostQuitMessage(0); return 0;
    }
    if (data && message == WM_NCDESTROY) {
        const LRESULT result = DefWindowProcW(hwnd, message, wparam, lparam);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
        data->state->window_final_destroyed = true;
        data->state->add(Event::WindowFinalDestroy);
        return result;
    }
    return DefWindowProcW(hwnd, message, wparam, lparam);
}
void window_thread(State& state) {
    state.ui_thread = GetCurrentThreadId();
    MSG queue{}; PeekMessageW(&queue, nullptr, 0, 0, PM_NOREMOVE);
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (initialized != S_OK && initialized != S_FALSE) {
        state.cleanup_failed = true; SetEvent(state.ready); return;
    }
    auto* provider = new Provider(state);
    // Heap callback storage survives a failed DestroyWindow/late proxy callback.
    // Its owned Provider reference is released only after final destruction.
    auto* data = new WindowData{&state, provider};
    WNDCLASSW klass{}; klass.lpfnWndProc = window_proc;
    klass.hInstance = GetModuleHandleW(nullptr); klass.lpszClassName = L"OpenAdaptOwnedProviderSource";
    const ATOM registered = RegisterClassW(&klass);
    HWND hwnd = registered ? CreateWindowExW(0, klass.lpszClassName, L"", WS_OVERLAPPED,
        0, 0, 64, 64, nullptr, nullptr, klass.hInstance, data) : nullptr;
    SetEvent(state.ready);
    int outcome = hwnd ? 1 : 2;  // early stop / failed creation; not normal WM_QUIT.
    if (hwnd && !state.stop.load()) {
        MSG message{};
        while (true) {
            const BOOL read = GetMessageW(&message, nullptr, 0, 0);
            if (read == 0) { outcome = 0; break; }
            if (read == -1) { outcome = -1; state.cleanup_failed = true; break; }
            TranslateMessage(&message); DispatchMessageW(&message);
        }
    }
    state.phase = 6;
    state.add(Event::PumpExit, outcome, outcome == 0 ? S_OK : E_FAIL);
    // Every pump exit reaches the SAME creator-thread destruction path.
    if (state.hwnd.load()) {
        const BOOL destroyed = DestroyWindow(state.hwnd.load());
        state.add(Event::WindowCleanup, 0, destroyed ? S_OK : E_FAIL);
        if (!destroyed) state.cleanup_failed = true;
    }
    const bool gone = state.window_final_destroyed.load() && state.hwnd.load() == nullptr;
    state.add(Event::WindowCleanup, 1, gone ? S_OK : E_FAIL);
    if (gone) {
        delete data; provider->Release();
        const BOOL unregistered = registered && UnregisterClassW(klass.lpszClassName, klass.hInstance);
        state.class_unregistered = unregistered != FALSE;
        state.add(Event::WindowCleanup, 2, unregistered ? S_OK : E_FAIL);
        if (!unregistered) state.cleanup_failed = true;
        const HRESULT clear_error = SetErrorInfo(0, nullptr);
        state.add(Event::ErrorSet, 1, clear_error);
        if (clear_error != S_OK) state.cleanup_failed = true;
        CoUninitialize();
    } else {
        // Do not release Provider or free GWLP_USERDATA while a window may use
        // it. This failed path remains UNQUALIFIED; all storage is retained to
        // process end under the mandatory outer Job. No apartment cleanup claim.
        state.cleanup_failed = true;
    }
    state.ui_completed = gone && state.class_unregistered.load() && !state.cleanup_failed.load();
    state.add(Event::WindowCleanup, 3, state.ui_completed ? S_OK : E_FAIL);
}
struct Snapshot {
    std::vector<Entry> entries;
    std::array<std::uint64_t, 24> event_counts;
    bool overflow, complete, cleanup_complete;
    unsigned forbidden, unknown, live_objects;
    ObservedScope issued;
};
Snapshot snapshot(State& state) {
    std::lock_guard<std::mutex> lock(state.mutex);
    const unsigned live = state.live_objects.load();
    const bool cleanup = state.ui_completed && state.window_final_destroyed &&
        state.class_unregistered && !state.cleanup_failed;
    // Under the valid owned COM reference contract, zero objects + joined UI
    // thread after both apartments close means no later valid callback exists.
    // Otherwise this is a locked stable PARTIAL snapshot, never a whole trace.
    return {state.entries, state.event_counts, state.overflow, cleanup && live == 0 && !state.overflow, cleanup,
        state.forbidden.load(), state.unknown.load(), live, state.issued_scope};
}
void emit_scope(const ObservedScope& observed) {
    std::cout << "{\"hwnd\":" << reinterpret_cast<std::uintptr_t>(observed.hwnd)
        << ",\"pid\":" << observed.pid << ",\"tid\":" << observed.tid << ",\"session\":" << observed.session
        << ",\"creation_low\":" << observed.creation.dwLowDateTime << ",\"creation_high\":" << observed.creation.dwHighDateTime
        << ",\"generation\":" << observed.fixture_generation << ",\"runtime_id\":[";
    for (std::size_t i = 0; i < observed.runtime.size; ++i) { if (i) std::cout << ','; std::cout << observed.runtime.values[i]; }
    std::cout << "]}";
}
void emit_trace(const Snapshot& state) {
    std::size_t ordinal = 0;
    for (const auto& entry : state.entries) {
        std::cout << "{\"role\":\"trace\",\"ordinal\":" << ordinal++ << ",\"event\":" << static_cast<unsigned>(entry.event)
            << ",\"argument\":" << entry.argument << ",\"hr\":" << static_cast<std::int32_t>(entry.status)
            << ",\"phase\":" << entry.phase << ",\"thread\":" << entry.thread
            << ",\"generation\":" << entry.generation
            << ",\"kind\":" << entry.kind << ",\"boolean_observed\":" << (entry.boolean_observed ? "true" : "false")
            << ",\"boolean\":" << entry.boolean << "}\n";
    }
}
}  // namespace

// Single case per proposed invocation: no recovery/re-find/retry.
bool parse(wchar_t* text, long maximum, long& value) noexcept {
    if (!text || !text[0] || text[0] < L'0' || text[0] > L'9') return false;
    wchar_t* end = nullptr; value = wcstol(text, &end, 10);
    return end && *end == L'\0' && value >= 0 && value <= maximum;
}
int wmain(int argc, wchar_t** argv) {
    long selected = -1, pattern = -1;
    if (argc != 3 || !parse(argv[1], 15, selected) || !parse(argv[2], 2, pattern)) return 90;
    // Keep callback storage alive even if a proxy unexpectedly retains an object.
    auto* state_ptr = new State; auto& state = *state_ptr;
    state.entries.reserve(1024); state.selected = static_cast<Case>(selected);
    if (selected >= 1 && selected <= 3) state.pattern = static_cast<PatternMode>(selected);
    if (selected >= 4 && selected <= 8) state.privacy = static_cast<PrivacyMode>(selected - 3);
    // A separate own-object getter oracle, retained in the WHOLE trace. No reset.
    auto* oracle = new ErrorTrap(state, true);
    GUID guid{}; BSTR text = nullptr; DWORD help = 0;
    oracle->GetGUID(&guid); oracle->GetSource(&text); oracle->GetDescription(&text);
    oracle->GetHelpFile(&text); oracle->GetHelpContext(&help); oracle->Release();
    state.ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    state.destroyed = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!state.ready || !state.destroyed) return 92;
    std::thread ui(window_thread, std::ref(state));
    const DWORD ready = WaitForSingleObject(state.ready, 5000);
    const HWND hwnd = state.hwnd.load();
    bool bound = false; cap::Observation observation; GuardReason reason = GuardReason::OwnerUnavailable;
    ObservedScope observed; bool observed_available = false;
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (ready == WAIT_OBJECT_0 && hwnd && (initialized == S_OK || initialized == S_FALSE)) {
        IUIAutomation* automation = nullptr;
        const HRESULT created = CoCreateInstance(__uuidof(CUIAutomation), nullptr,
            CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&automation));
        if (created == S_OK && automation) {
            DWORD session = 0;
            if (ProcessIdToSessionId(GetCurrentProcessId(), &session)) {
                Checkpoint checkpoint;
                ExpectedOwner expected{hwnd, GetCurrentProcessId(), session, state.generation.load()};
                if (state.selected == Case::WrongSession) expected.session ^= 1;
                if (state.selected == Case::WrongGeneration) ++expected.fixture_generation;
                bound = checkpoint.bind(automation, expected, generation, &state, guard_trace, &state);
                if (bound) {
                    observed = checkpoint.observed_scope(); observed_available = true;
                    if (state.selected == Case::Expired) checkpoint.expire_for_owned_control();
                    observation = cap::observe_capability(checkpoint.borrowed_element(), static_cast<cap::Pattern>(pattern),
                        cap::kSourceAbi, checkpoint.sdk_scope(), Checkpoint::callback, &checkpoint);
                }
                reason = checkpoint.reason(); state.phase = 6;
            }
            automation->Release();
        }
    }
    if (initialized == S_OK || initialized == S_FALSE) {
        const HRESULT clear_error = SetErrorInfo(0, nullptr);
        state.add(Event::ErrorSet, 1, clear_error);
        if (clear_error != S_OK) state.cleanup_failed = true; CoUninitialize();
    }
    state.stop = true;
    if (state.hwnd.load()) PostMessageW(state.hwnd.load(), WM_CLOSE, 0, 0);
    else if (state.ui_thread.load()) PostThreadMessageW(state.ui_thread.load(), WM_QUIT, 0, 0);
    // A hung call cannot earn a clean receipt. The REQUIRED retained outer Job
    // terminates this whole owned process at 20s and preserves partial capture.
    ui.join(); CloseHandle(state.ready); CloseHandle(state.destroyed);
    const Snapshot retained = snapshot(state);
    emit_trace(retained);
    const bool trace_clean = retained.complete && retained.forbidden == 0 && retained.unknown == 0 && !retained.overflow;
    bool expected = false;
    switch (state.selected) {
    case Case::Supported: expected = bound && reason == GuardReason::None && observation.capability == cap::Capability::Supported; break;
    case Case::NormalNull: case Case::ExplicitUnsupported: case Case::PatternError:
        // Server intent does not determine the client HRESULT/category.
        expected = bound && observation.capability != cap::Capability::Supported; break;
    case Case::Sensitive: expected = !bound && reason == GuardReason::Sensitive; break;
    case Case::UnsupportedPrivacy: case Case::WrongPrivacyKind: case Case::PrivacyError:
        expected = !bound && reason == GuardReason::PrivacyUnknown; break;
    case Case::WrongSession: expected = !bound && reason == GuardReason::OwnerMismatch; break;
    case Case::WrongGeneration: expected = !bound && reason == GuardReason::ScopeChanged; break;
    case Case::Expired: expected = bound && reason == GuardReason::Deadline && !observation.acquire_attempted; break;
    case Case::SensitiveBeforeAcquire: expected = bound && reason == GuardReason::Sensitive && !observation.acquire_attempted; break;
    case Case::SensitiveBeforeQI: expected = bound && reason == GuardReason::Sensitive && observation.acquire_attempted && !observation.query_attempted; break;
    case Case::SensitiveAfterQI: expected = bound && reason == GuardReason::Sensitive && observation.query_attempted && observation.capability == cap::Capability::Unknown; break;
    case Case::DestroyedBeforeAcquire: expected = bound && reason == GuardReason::OwnerMismatch && !observation.acquire_attempted; break;
    case Case::EmptyFallback: break; // Observational-only, never a certified Unknown oracle.
    }
    std::cout << std::boolalpha << "{\"role\":\"result\",\"case\":" << selected << ",\"pattern\":" << pattern
        << ",\"bound\":" << bound << ",\"guard_reason\":" << static_cast<unsigned>(reason)
        << ",\"capability\":" << static_cast<unsigned>(observation.capability)
        << ",\"capability_reason\":" << static_cast<unsigned>(observation.reason)
        << ",\"acquire_attempted\":" << observation.acquire_attempted
        << ",\"acquire_hr\":" << observation.acquire_hresult << ",\"acquire_nonnull\":" << observation.acquire_nonnull
        << ",\"query_attempted\":" << observation.query_attempted
        << ",\"query_hr\":" << observation.query_hresult << ",\"query_nonnull\":" << observation.query_nonnull
        << ",\"forbidden\":" << retained.forbidden << ",\"unexplained\":" << retained.unknown
        << ",\"trace_overflow\":" << retained.overflow << ",\"live_objects\":" << retained.live_objects
        << ",\"trace_complete\":" << retained.complete << ",\"cleanup_complete\":" << retained.cleanup_complete
        << ",\"trace_clean\":" << trace_clean << ",\"expected_outcome\":" << expected
        << ",\"observed_scope\":";
    if (!observed_available) std::cout << "null"; else emit_scope(observed);
    std::cout << ",\"oracle_scope\":"; emit_scope(retained.issued);
    std::cout << ",\"event_counts\":[";
    for (std::size_t i = 0; i < retained.event_counts.size(); ++i) { if (i) std::cout << ','; std::cout << retained.event_counts[i]; }
    std::cout << "],\"runtime_adoption\":false}" << std::endl;
    const bool passed = expected && trace_clean;
    if (retained.complete) delete state_ptr;
    return passed ? 0 : 94;
}
