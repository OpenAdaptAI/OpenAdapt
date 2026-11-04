"""OpenAdapt as the scorer for Harness² on GUI tasks.

Harness² (google-research/harness2) searches over an agent's harness. On its
own benchmarks there is no ground-truth scorer, so a model judges. Here every
rollout runs against a fresh copy of a local app, and the reward worker reads
that copy's system of record afterwards: a verified effect, a record on the
wrong patient, a duplicate, or a safe stop.

Modules:

* ``tasks``: :class:`~.tasks.GuiTask` and the MockMed task family.
* ``apps``: resettable app targets (MockMed today).
* ``scorer``: the reward worker over one app copy, plus the search scalar.
* ``agent``: the GUI agent (scripted, or a model) and its page driver.
* ``rollout``: one rollout from fresh copy to signed receipt.
* ``adapter``: the Harness² ``BenchmarkAdapter`` (imports Harness²).
* ``selection``: choose the final by reward; run one task end to end.
* ``proposers``: scripted and single-call model proposers for its loop.
* ``llm``: a small HTTP chat client with a hard spending cap.

Only ``adapter``, ``selection`` and ``proposers`` import Harness². See
``docs/HARNESS2.md``.
"""
