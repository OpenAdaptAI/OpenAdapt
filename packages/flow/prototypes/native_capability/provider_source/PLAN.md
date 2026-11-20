# Owned provider qualification candidate

This is **source only**. No file here has been compiled against the Windows SDK,
launched on Windows, or used to query a provider. The local tests manufacture
category records and fake process results. They cannot qualify the C++ ABI,
Windows privacy or identity observation, UIA core, or a real application.

The durable item is an independently inspectable reason to refuse incomplete
observations before they can authorize a record change. There is no per-app
engine code or new runtime integration. One closed model covers Toggle,
SelectionItem and Value capability observations. No held-out application result
is claimed.

## Immutable inputs and package boundary

The parent is `8114d9a8680c261c2252c083dac0b0a196820462`. Its SDK subject is the
unchanged `b0ab00afcba6d3f78890767227b1bd9241f27b22` implementation. PR2291 remains
frozen at `1a2503da4e240ba24620845cf85737d087c38e39`; this candidate neither changes
that source nor retries its failed projection. The earlier captured 64-case
owned direct-COM result is historical evidence about that subject, not this new
provider, guard or binding.

The existing 8114 runner is loaded only from its exact sibling source, SHA256
`6ed593c74f06518d4e9c7fc73626a362b8da1b1ee30ec53b635ebcd93062ed39`.
It supplies the same explicit split-stream capture, retained Windows Job,
START gate, headless child and cleanup checks. Its process utility is still
pinned to SHA256
`c731e2d758c98d7be512be487e81161cc81847f9ac15082a80504ea729758b90`.
No installer, browser, retry, runtime factory or system provider discovery path
is called. The existing wheel includes only `openadapt_flow`; the existing
sdist excludes `/prototypes`. A local test parses those actual selectors and
checks all frozen subject/harness hashes. No package or workflow file changes.

The historical inventory was MSVC14.40.33807 / cl19.40.33811 and SDK10.0.22621.0.
A future attempt must inventory the actual available compiler/SDK again under
a new authorized lease and retain their hashes. The proposed command pins the
invoked compiler path and SDK, clears only child CL/_CL_/LINK/_LINK_, and
records source/toolchain hashes before and after. This is not a complete
compiler/linker/OS-image closure. The actual loaded UIA core/proxy image,
version, apartment and provider freshness are **not yet qualified**.

## Minimal native boundary

`sdk_checkpoint.{h,cpp}` is a fixture-only checkpoint adapter. It retains its
own process handle (query-information + synchronize) and one client element
reference. It uses only the issued HWND; there is no root/tree scan, Find,
caption, AutomationId, expected-ID substitution or recovery re-find.

It observes the process PID, creation FILETIME, session, live-handle wait state,
window PID/TID, the complete opaque integer RuntimeId array and the actual own
window lifecycle generation. The independent provider thread retains its own
issued native tuple; the auditor compares that tuple with the client guard's
observed tuple. RuntimeId is observed, never predicted or decoded. Unknown,
empty, non-INT/unsupported arrays, more than64 integers, changed generation,
owner mismatch or expired deadline refuse. The limit is a refusal bound, not a
claim that all valid providers have short runtime IDs. HWND and RuntimeId can
be reused; the fixture generation is an own-fixture oracle, not a general
business identity or future write permission.

At every frozen SDK checkpoint, native owner checks precede an explicit raw
privacy read; provider identity metadata follows it; privacy is read again
LAST among provider operations, followed only by fresh native owner checks.
The raw call is:

```cpp
GetCurrentPropertyValueEx(UIA_IsPasswordPropertyId, TRUE, &initialized_variant)
```

Only exact S_OK, exact VT_BOOL and canonical VARIANT_FALSE permit a probe.
Canonical VARIANT_TRUE refuses sensitive. Unsupported, VT_EMPTY, wrong type,
noncanonical BOOL, non-S_OK, missing owner/runtime or cleanup failure refuses.
There is no convenience `CurrentIsPassword`, coercion or default-FALSE path.
VariantClear and owned Release are cleanup, not an authorization fallback.
The trace retains signed HRESULT, VARTYPE and canonical BOOL observation, never
BSTR/object contents, exception strings or provider repr. Checks are discrete
observation points; they are not an atomic or continuous global identity proof.
A callback deadline cannot interrupt a hung COM call; the outer retained Job
must terminate the owned process and retain partial output in that case.

## Instrumented server and independent oracle

`provider_fixture.cpp` implements the SDK SERVER interfaces
IRawElementProviderSimple, IToggleProvider, ISelectionItemProvider and
IValueProvider. It does not reuse the previous fake CLIENT pattern objects.
One hidden own window serves only the original WM_GETOBJECT arguments through
UiaReturnRawElementProvider, with UiaHostProviderFromHwnd for its host provider.
The provider/message thread is STA; the client is a nonwindow MTA thread.
ProviderOptions claims ServerSideProvider only. Hidden-window discoverability,
COM marshaling, QI behavior and cleanup have not been demonstrated. Failure
will be retained; no ShowWindow, focus, search or alternate-provider fallback
is proposed.

All subject bootstrap, property/pattern requests and outputs, QI, AddRef,
Release, guard phases, window generation and cleanup events remain in one
ordinal trace. It is never reset. Every subject semantic/action entry, Name,
Value, Text, Legacy/window-text request or error-info getter is forbidden;
unknown interface/property/pattern activity or trace overflow also makes the
case unqualified. The own provider returns no content. UIA core may itself
make unexpected calls; those calls are evidence of a failed boundary and will
not be ignored as bootstrap noise.

A separate own IErrorInfo oracle deliberately calls all five non-content
stub outputs before bootstrap; its five events remain explicitly labelled in
the same trace. Subject traps count separately, without resetting counters.
SetErrorInfo outcomes and cleanup are retained. Zero subject getters can bound
only the instrumented own objects. It cannot prove that UIA core/proxies never
called GetErrorInfo or another object's accessors, and there is no cross-
apartment error-info privacy qualification yet.

The provider's reserved-unsupported privacy case returns VT_UNKNOWN with
UiaGetReservedNotSupportedValue. Its **actual** output category and the client's
**actual** raw category remain separate. S_OK/VT_EMPTY permits host fallback,
so case8 is deliberately observational and can never pass as a certified
Unknown. Provider S_OK/null does not determine the client HRESULT or establish
business-state absence. The unchanged SDK subject keeps client S_OK/null
Unknown. Explicit UIA_E_NOTSUPPORTED/null describes only that one capability;
it does not prove the three patterns represent complete business state.

## Closed cases and pure controls

All16 selectors are defined once and apply to each of the three SDK patterns:

| Case | Controlled own input | Required observation bound |
| --- | --- | --- |
|0|Supported interface|Bound, exact typed acquisition/QI, Supported, all final checkpoints|
|1|Provider normal null|Actual non-Supported client outcome; normal client null stays Unknown|
|2|Provider explicit unsupported|Retain actual client HRESULT; no invented absence|
|3|Provider failure|Retain actual client failure; no Supported result|
|4|Password true|Sensitive refusal before capability probe|
|5|Reserved unsupported privacy|Unknown refusal before capability probe|
|6|Wrong privacy VARIANT type|Unknown refusal before capability probe|
|7|Privacy HRESULT failure|Unknown refusal before capability probe|
|8|VT_EMPTY privacy|Observational/unqualified regardless of default outcome|
|9|Wrong expected session on the same owned process|Owner mismatch, no foreign session/account|
|10|Wrong expected generation|Scope mismatch, no substituted observed generation|
|11|Expired checkpoint|No acquisition|
|12|Sensitive before acquisition|No acquisition|
|13|Sensitive before exact QI|Acquisition may have occurred, no QI|
|14|Sensitive after QI|Unknown, only owned cleanup follows|
|15|Actual own window destruction before acquisition|Owner mismatch, no acquisition/re-find|

The separate Python auditor validates strict closed row shapes/types, complete
raw scope, the live five-getter oracle, exact guard-stage order, provider
request/output census and first-refusal short circuit. It does not trust the
fixture's `expected_outcome` flag or substitute configured IDs for observations.
A positive observation requires all three recorded layers to agree: case
manifest, actual server trace and actual client result. These unsigned local
records are not a qualification credential or runtime authority.

The local unittest command is:

```sh
~/oa/scratch/cpu-slots/run.sh <existing-python> -I -S -B \
  packages/flow/prototypes/native_capability/provider_source/test_provider_source.py
```

Its generated48 case/pattern records, raw-type lattice and planted metadata,
bootstrap, missing/duplicate, hidden suffix of trace, post-refusal getter,
wrong scope, empty-capture, empty-compiler and unknown-Job faults are PURE
controls. No native compile, provider or Flow import occurs. They do not test
the C++ implementation or prove its Windows behavior.

## Proposed future execution, currently prohibited

The first future attempt would use one freshly reviewed exact source tree and
ONE case/pattern per process. A positive Toggle case is the smallest initial
check; it does not qualify the other patterns or negative matrix. Further
cases need a concrete bounded manifest and a separate root decision. No retry
or automatic alternate host/provider is built in.

The inert single-case command prepared for review is:

```text
C:\Python312\python.exe -I -S provider_source\run_provider_source.py --sdk 10.0.22621.0 --process-support <exact-reviewed-install_playwright_browser.py> --case 0 --pattern 0
```

A future private DIRECT launcher must name its fresh own root and exact source
hashes, retain stdout/stderr without content expansion, and use the existing
native-lane exclusive lease/QUEUE/REBOOTING procedure. No WSL queue or gate is
part of this lane. Mac CPU-slot-wrapped bounded SSH would retain the parent
180s process and240s transport cap. The inner work budget remains130s: exact
split-stream I/O positive5s; compile90s with nonempty /Bv output; ONE provider
case20s. Each child uses the unchanged START/Job/no-window path. Any nonzero,
timeout, stderr, missing capture, unexpected trace or unconfirmed Job stops the
pipeline. Parent/Job stop proof precedes owned-tree cleanup and lease release;
unknown cleanup retains the lease and is reported. No broad process search,
kill by name, system setting, installation, secret copy, DB, agent, RDP/HTTP,
real app, cloud resource or runtime adoption is needed.

No remote launcher has been created or executed for this candidate. Exact
source/API review and an explicit root execution decision must come first.
No PR, workflow dispatch or private paid trigger is authorized. Source/evidence
preservation is branch-only after unchanged actual event-filter/no-PR proof,
with exact-SHA postpush zero-run readback.

## Primary contracts consulted

- [Raw property reads and ignoreDefault](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getcurrentpropertyvalueex).
- [Provider property fallback and reserved unsupported](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationcore/nf-uiautomationcore-irawelementprovidersimple-getpropertyvalue).
- [VARENUM and canonical VARIANT_BOOL](https://learn.microsoft.com/en-us/windows/win32/api/wtypes/ne-wtypes-varenum).
- [Opaque/reusable RuntimeId](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getruntimeid).
- [UIA threading](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading).
- [Process session access rights](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-processidtosessionid).
- [Server provider interface](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationcore/nn-uiautomationcore-irawelementprovidersimple).
- [WM_GETOBJECT provider return](https://learn.microsoft.com/en-us/windows/desktop/api/UIAutomationCoreApi/nf-uiautomationcoreapi-uiareturnrawelementprovider).
- [Error-info TLS transfer](https://learn.microsoft.com/en-us/windows/win32/api/oleauto/nf-oleauto-geterrorinfo) and [replacement lifetime](https://learn.microsoft.com/en-us/windows/win32/api/oleauto/nf-oleauto-seterrorinfo).

SDK declarations were inspected from Microsoft/win32metadata commit
`9cf94833626504b330c2f616ce71a2d3497cdc76`; UIAutomationCore.h readback SHA256
`9a222d488f56a1357bf2951c48d8efba8004a8c24587ae6f30975fc51f01bf1c`.
No SDK implementation/header is vendored. The Windows peer's read-only API
inventory informed these boundaries; independent review of this candidate is
still pending.
