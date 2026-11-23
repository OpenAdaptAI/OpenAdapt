## OpenAdapt in ten capabilities

Each capability is implemented in the
[OpenAdapt monorepo](https://github.com/OpenAdaptAI/OpenAdapt). "Released"
means it ships in the current PyPI packages; "on main" means merged into the
monorepo and installable from source.

1. **Learns from any successful run, a person's or an agent's.** A person
   records a task, or a computer-use agent does it once and OpenAdapt compiles
   the verified run into a program (`openadapt_flow.jit`, released;
   prompt-to-program `compile-agent`, on main; the agent acts through code by
   default, not screenshots, and every save the gate detects in its run is read
   back, [#1305](https://github.com/OpenAdaptAI/OpenAdapt/pull/1305)). Agents can drive the recording
   through the web, native Windows, RDP and Citrix backends. An agent connected over MCP can
   teach itself a new workflow and then call it as a tool
   (`openadapt-agent serve --teach`,
   [#1335](https://github.com/OpenAdaptAI/OpenAdapt/pull/1335)).
2. **Compiles to the fastest layer the app allows.** Page structure first (DOM,
   Windows UI Automation, macOS Accessibility), direct API calls where the app's
   own traffic allows them (`ApiActuator`, released; compiled from captured
   traffic in [#1261](https://github.com/OpenAdaptAI/OpenAdapt/pull/1261)),
   and pixels only
   where nothing else exists, such as a remote desktop. It is not a pixel-macro
   recorder.
3. **Builds branching programs, not one linear path.** One program from several
   demonstrations (`induce`, `disambiguate`), a branch learned from several
   halts fixed the same way (`teach-branch`), candidate branches mined from many
   real runs in the background (shadow mode,
   [#1212](https://github.com/OpenAdaptAI/OpenAdapt/pull/1212)), and a router
   that sends each item to the right verified variant. Shadow mining and the
   router are on main; the rest is released.
4. **Works across surfaces in one workflow.** Backends for web, native Windows
   and macOS, Linux, RDP and Citrix. Programs chain across surfaces passing confirmed values
   (`compose`, `process`, released), and one program can switch surfaces, tabs
   and popups mid-run ([#1197](https://github.com/OpenAdaptAI/OpenAdapt/pull/1197)).
5. **Confirms the right record before every write and reads it back after.**
   A run is `VERIFIED` only when a read outside the acting session confirms the
   save: the system of record (API, database, FHIR with SMART Backend Services,
   a log or an export) or a separate session (a fresh sign-in on the web; on
   desktop, a fresh session or a relaunch of the app, as the deployment
   declares). A read on the acting screen gets its own level and never
   verifies a run. The run report names the level for each save with a
   declared read-back. Where the app's own screens are the only independent
   read, the check covers at most the target record, plus the same actor's
   changes in the app's activity log within a stated window where the app
   shows one and the program is set up to read it; other records, later
   server-side jobs and outbound messages are not covered. The wire check is
   evidence of what was sent, not a check level.
   `record --verify` proposes those checks from what the demonstration changed.
   Released.
6. **Stops instead of improvising, and never retries an uncertain save.** A run
   that can't prove a save ends `HALTED_BEFORE_EFFECT` or
   `RECONCILIATION_REQUIRED`, never "success". A ledger of what changed before
   a stop, with undo steps a person approves, is on main. The rest is
   released.
7. **Approvals are bound to exactly what will be saved.** Each save waits for a
   person by default and can be answered on a paired phone with one card per
   decision. The approval is bound to the exact screenshot and form state, and
   the form is read again before saving, so what was approved is what is saved.
   Person-signed grants let named agents answer. Released.
8. **Repairs itself, with a person approving.** Governed repair (propose,
   review, campaign, canary, rollback), agent-demonstrated fixes for a halted
   run, and fleet drift patches shared with no patient data (moved as files
   or through a local reference hub) are released.
   Self-repair, where an agent proposes a fix and Flow probes it up to the save
   without saving, is on main, and so are fixes of up to three new steps.
   Self-repair is qualified on browser runs ([benchmark/auto_repair](packages/flow/benchmark/auto_repair/README.md))
   and end to end on native macOS AX ([#1715](https://github.com/OpenAdaptAI/OpenAdapt/pull/1715)). Outside a browser it
   runs with `repair.session` ([#1717](https://github.com/OpenAdaptAI/OpenAdapt/pull/1717)), and native and pixel backends
   admit its new clicks by element identity ([#1709](https://github.com/OpenAdaptAI/OpenAdapt/pull/1709)). Teaching the fix
   for a halted run (`--hold-on-halt`) and rollback are tested on browser runs.
9. **Is a safety layer underneath any agent.** An agent's MCP calls, a task
   ledger that keeps a long job's rules, items, questions and checked saves on
   the server ([#1332](https://github.com/OpenAdaptAI/OpenAdapt/pull/1332)), a guarded save around any agent's write
   ([#1258](https://github.com/OpenAdaptAI/OpenAdapt/pull/1258)), a guard for
   an agent you already run that holds its browser's saves with no change to
   the agent ([#1357](https://github.com/OpenAdaptAI/OpenAdapt/pull/1357)), `verify-run`
   to judge any executor's write from the system of record, and Agent Skills and
   a CLI for coding agents. Released except where a PR is named.
10. **Runs locally and keeps data private.** Healthy runs make no calls to a
    hosted AI model and send nothing out. Personal and health data are redacted from text,
    screenshots and structure (scrub-by-default for anything that leaves the
    machine is on main); a strict no-outbound mode, an unattended on-prem
    service, and signed, timestamped receipts are released.

