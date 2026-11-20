# Owned COM ABI fixture: pre-execution plan

This is a separate child of immutable source prototype b0ab00af. Its README
records that proposal-time scope; source presence here grants no native result. It does not
change that prototype, current Windows-agent source or any runtime consumer.
It tests direct calls on first-party in-process objects deriving from the SDK's
IUIAutomationElement and Toggle/SelectionItem/Value interfaces. It never creates,
registers, locates or calls a live UIA provider, element, desktop or application.
No content/action/property stub is invoked. All unneeded interface methods are
own E_NOTIMPL implementations with an unexpected-call counter.

The declaration signature inventory follows Microsoft's public
[UIAutomationClient.h](https://github.com/microsoft/win32metadata/blob/9cf94833626504b330c2f616ce71a2d3497cdc76/generation/WinSDK/RecompiledIdlHeaders/um/UIAutomationClient.h).
The interface header/implementation is not vendored. The compiler uses its
existing installed SDK definitions and override checking, not handwritten
vtable slots, invented addresses or copied provider code. The source inventory
has82Element methods; GetCurrentPatternAs is implemented explicitly and81are
closed stubs. The three patterns have15closed semantic stubs in total.

## Planned subject and cases

`run_abi_fixture.py` checks the exact original b0 C++/header hashes before build.
It requires a new owned output directory, the inventoried existing compiler
setup and SDK10.0.22621.0. It installs nothing and imports no Flow package.
`compile_fixture.cmd` changes only its child compiler environment, clears inherited
CL/_CL_/LINK/_LINK_ option injection, checks selected SDK/MSVC versions and records
actual compiler selection plus /Bv output. Compiler and SDK-header hashes are
recorded before/after. The built executable's hash is retained.

The planned64subject cases cover all three patterns: exactS_OK support, explicit
UIA_E_NOTSUPPORTED/null, normalS_OK/null, S_FALSE, generic failure, contradictory
valid owned output, QI failures/null/non-success, each checkpoint refusal and
preflight mismatch/missing input. All HRESULTs, attempted flags, exact reason,
output flags, actual refcounts/releases, alias Boolean and stage sequence are
checked and recorded. QI/acquisition references are independently owned even
when interface addresses alias. No pointer addresses are exported. Synthetic
failure outputs remain valid SDK-derived releasable objects; no dangling or wild
pointer is fabricated. The caller retains live owned objects throughout.

A separate positive oracle calls all five getters on OUR IErrorInfo trap; each
increments its own counter, returns a closed failure and supplies no text.
Every subject case uses a fresh trap. Setup and injected-fault SetErrorInfo
results, subject getter deltas, TLS trap-survival/identity, explicit GetErrorInfo
transfer and cleanup are recorded separately. GetErrorInfo consumes the TLS
reference; it is released once. Original caller refs are released and all owned
objects must be destroyed. No provider error strings or exception repr is read.
Trap survival and zero getters bound this direct same-thread own-object path;
they do not prove absence of every GetErrorInfo call or proxy/cross-apartment
privacy. The fixture initializes/uninitializes its own COM thread only.

## Exact local Windows commands

Run from a new owned copy of this directory, with the existing compiler path:

```text
C:\Python312\python.exe -I -S run_abi_fixture.py --process-support install_playwright_browser.py --sdk 10.0.22621.0
```

The runner invokes these three child operations, in order. The first is a
separate fixed-I/O guard, never an extra fixture case:

```text
C:\Python312\python.exe -I -S io_capture_control.py
C:\Windows\System32\cmd.exe /d /c compile_fixture.cmd <existing-vcvars64.bat> 10.0.22621.0 <exact-hashed-cl.exe>
out\fixture.exe
```

The compiler command is in the checked-in batch file, using the original b0
source plus own fixture, console subsystem and static CRT. It invokes the exact
hashed compiler path, not PATH selection. Before compilation, exact owned stdout/stderr literal bytes must be captured
in SEPARATE files, with observed lengths/hashes and a proved empty/closed/reaped
Job. Missing, empty, partial, extra or unconfirmed capture refuses before compile.
The shared gated child forwards stdout/stderr handles explicitly under
CREATE_NO_WINDOW. Compile failure or an empty /Bv compile log refuses before
fixture execution; no normalization or exit-zero substitution is allowed. The existing unchanged `scripts/install_playwright_browser.py` process
support is copied with its original name and checked by exact SHA before loading.
Only its kernel32 retained Windows Job class is used;
installer/browser/retry entrypoints are never called. This kernel32 process-only
utility is separate from any UIA/COM capability binding.

Both children run through a tiny own START-only `python -I -S` entry which
explicitly passes CREATE_NO_WINDOW to the single planned child. The parent
wrapper also has CREATE_NO_WINDOW; the reused installer/browser CLI is never run.
A wrapper is assigned to the retained Job before START permits any real child.
The existing Job uses KILL_ON_JOB_CLOSE, bounded terminate/ActiveProcesses==0
verification and handle closure. Finally closes the own pipe, terminates/verifies
the job, reaps the own wrapper and records all outcomes. An unassigned wrapper
never received START and may be stopped only through its retained Popen handle.
No taskkill, process-name/PID search or new process manager is used. Even normal
leader exit requires the job to be empty, not just the leader's exit status.

Fixed-I/O wait is5seconds, compiler wait90seconds and fixture wait20seconds;
work deadline130seconds refuses
any late phase. Each phase has at most the existing5second Job-verification and
5second wrapper-reap allowances, for at most160seconds of bounded waits across three phases; the outer owner
launcher remains180seconds and SSH240seconds.
OS creation/file operations themselves are not proven hang-free by this plan.
Each wrapper's owned PID and state is written before START/wait. Unconfirmed job
empty/close/wrapper cleanup is a closed failure, never success or permission to
continue. Keep the lease on an unconfirmed stop and ask the coordinator for
cleanup disposition; no replacement command or retry follows.

Use the documented exclusive native Windows lease/QUEUE/REBOOTING rules and
wrap the bounded SSH capture in the Mac CPU-slot wrapper. The WSL remote-test
slot gate belongs to a different lane and is not invoked. Copy only the
eight named subject/fixture source files plus the unchanged process-support file; no environment, credentials, keys, runtime package or
lab-agent file. The direct-native plaintext launcher, its retained hash, outer SSH timeout
and exact owned root are reviewed privately before execution. No WSL command or
queued WSL workload is created. The existing remote sync/install launcher is not used, because it installs
dependencies; only the exact-hash isolated Job primitive/wrapper is reused. No lab reboot/settings/agent start occurs.

## Limits and retention

This plan and its source are reviewed before any native build/execution. The
existing143model tests and new pure runner tests use fake processes only. Those
results are not ABI evidence. After the one authorized native attempt, stop on
any refusal or new finding; preserve actual logs/partial JSON and do not retry.
Record source/toolchain/binary identity, every actual case and cleanup, then
release the own lease only when no own process remains. Private evidence pushes
must satisfy the existing unchanged non-main/no-PR zero-trigger rules and actual
postpush run readback; no paid CI/PR/workflow action is part of this fixture.

At most this can qualify the tested compiled SDK/HRESULT/refcount/error-info path
on these own objects. It cannot qualify real pattern absence, sensitivity guards,
whole state/value, cross-apartment/proxy behavior, live providers, held-out apps,
current approval/record effects or end-to-end execution. No loader, factory,
fallback, runtime adoption, public PR, workflow change or release is authorized.
