# Capability source prototype

This candidate distinguishes supported, explicitly unsupported and unknown
pattern acquisition. It implements no content or semantic-state reader. It is
not used by the Windows agent, complete-value helper, approval path or verifier.
There is no native loader, factory, public package export or fallback after an
existing observation refuses. Flow's wheel package selectors exclude this tree;
the sdist excludes `/prototypes` explicitly. The launcher has narrow package and
sdist selectors. Tests inspect these actual selectors and runtime source imports.

`capability_model.py` is a pure synthetic model. `sdk_capability.cpp` and its
header are **uncompiled source** using the installed Windows SDK declarations.
No SDK or third-party implementation is copied here. No DLL, native address,
provider, element or interface has been invoked or qualified by this prototype.

The defect class is loss of acquisition outcome and coverage evidence: wrapper
`None`, managed false, unavailable/defaulted properties, normal null and errors
can look like absence. The same resolver handles Toggle, SelectionItem and Value.
It does not infer absence from an application's name, control type or label.

## Source boundary

The C++ call uses `GetCurrentPatternAs` with caller-owned, initially null, typed
output storage and `IID_PPV_ARGS`. Exact-IID QueryInterface uses the same SDK
type. This avoids handwritten vtable offsets, IID literals and automatic Python
COM exception conversion. The shim does not call error-info/string getters.
It returns signed 32-bit HRESULTs and attempted flags, with no pointer addresses.
The direct-call result does not carry scope/pattern bindings and is **not a
serialized receipt or proof**. Any eventual transport requires a separately
reviewed closed binding/version contract; the prototype ABI is not that contract.

Only exact S_OK, non-null typed acquisition and successful exact-IID QI may yield
Supported after fresh checkpoints. Exact acquisition UIA_E_NOTSUPPORTED with null
output may yield ExplicitlyUnsupported for that pattern only. Normal S_OK/null,
S_FALSE, other HRESULTs, contradictory output, failed QI and stale/failed guards
remain Unknown. QI E_NOINTERFACE is not pattern absence. Unknown short-circuits;
only mandatory ownership cleanup follows refusal, with no diagnostic getter.

The callback seam must freshly enforce the existing supported raw sensitivity,
fixed identity, scope, deadline and protocol contract without rich-error or
content reads of its own. It is unimplemented and unqualified. Its Boolean return
is not a runtime permission or a qualification flag. The caller must hold a live
bound element reference in the correct COM apartment throughout the operation;
callbacks must not release, replace or invalidate it. No implicit element search
or Python/managed wrapper is supplied here.

Acquisition and successful QI own separate references even when their addresses
alias. Each acquired reference is released once on every ordinary return. Failed
HRESULT plus non-null output is refused and cleaned, assuming that output is an
ABI-valid releasable interface. The source does not validate wild pointers,
survive provider memory corruption or establish native exception safety. Callback
noexcept behavior and COM Release behavior remain ABI/privacy assumptions.
Checkpoint deadline checks can reject a late result; they cannot bound a hung COM
call. The Python invalid-output/release-exception cases are synthetic controls,
not proof of native pointer validation or exception handling.

## Capability and coverage stay separate

A supported pattern does not establish complete business state, a complete value
or write authority. Required absent patterns, unknown observations, supported but
unrepresented channels and additional relevant/composite channels refuse the
model's coverage check. Three absences are not a universal state census. An input
CoverageClaim is only a model input; no qualified coverage producer is provided.
`model_consistent` means necessary abstract conditions hold, not complete,
verified, maskable or qualified. No result is converted into a runtime field,
mask, identity, approval or effect proof.

Value support still requires the independent complete-value/extent/identity
contract before any later semantic value could be used. Read-only is orthogonal.
No unsupported channel is supplied by Name, MSAA, OCR, default False or another
fallback. Runtime adoption must use a newly qualified primary protocol and never
run this observer after an old refusal.

## Checks and remaining qualification

Run the pure controls with the existing test dependencies:

```sh
python -m pytest -q -p no:cacheprovider prototypes/native_capability/test_capability_model.py
```

Use the workspace CPU-slot wrapper on a shared machine. The family includes
three-pattern acquisition/QI/guard/cleanup controls, a 1,728-state abstract
required/representation matrix, HRESULT properties and scope-generation stateful
controls. Counts describe synthetic model cases, not ABI or application tests.

Before any adoption, separately authorize and qualify: the actual compiler/SDK,
ABI/calling convention/pointer width/ownership/apartment binding; typed successful
and failed outputs; zero rich-error-accessor calls including callback/QI/cleanup
paths; bounded-call behavior; real positive and negative provider support for all
three patterns with an independent oracle; complete state/value coverage; strict
old/new protocol refusal; held-out and approval/record-effect composition. None
of those native, provider, held-out or end-to-end qualifications has run here.

Primary interface references:

- [GetCurrentPatternAs](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getcurrentpatternas)
- [UI Automation error codes](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-error-codes)
- [QueryInterface](https://learn.microsoft.com/en-us/windows/win32/api/unknwn/nf-unknwn-iunknown-queryinterface%28refiid_void%29)
- [Pattern identifiers](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controlpattern-ids)

Durable item: capability uncertainty cannot silently hide business state.
No per-app engine code is added. One model covers the three siblings. Evidence
is synthetic/source only; no held-out application or native qualification exists.
