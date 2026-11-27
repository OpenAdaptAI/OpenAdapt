# Proposed website copy: check scope without database access

Status: draft for approval. Nothing here is applied to the website.

These changes bring the insurance and dental pages in line with the repository
docs ([LIMITS.md](../packages/flow/docs/LIMITS.md#what-the-guarantee-covers-without-database-access),
[READBACK_LADDER.md](../packages/flow/docs/READBACK_LADDER.md),
[THREAT_MODEL.md](../packages/flow/docs/THREAT_MODEL.md#what-layer-1-covers)).
After each action, OpenAdapt checks observable evidence and stamps how the
action was checked (`same_screen`, `reopened`, `separate_session`, or
`system_of_record`) on the receipt: right on the current screen by default
(working even on in-place edits with no separate post-save screen and on
non-idempotent portals that cannot reload), or via a reopened record view, a
separate read-only session, or a direct API, database, or file check when
available.

File: `data/site/pages/industries.mjs` in `OpenAdaptAI/openadapt-web`, at
commit `53f05758`. Line numbers refer to that commit.

## Line 145: insurance, who it's for (make conditional)

Current:

> It's also for brokers and agencies that key claims or submissions into
> carrier portals. There, the check opens the saved claim again in a fresh
> browser session and compares it field by field with what was entered. That
> check is set up along with the workflow.

Proposed:

> It's also for brokers and agencies that key claims or submissions into
> carrier portals. OpenAdapt checks observable evidence after each step: on
> the current screen by default (including in-place edits and portals that
> cannot reload), or by reopening the claim, checking in a separate read-only
> session, or reading an API or export when available, and stamps the exact
> check level (`same_screen`, `reopened`, `separate_session`, or
> `system_of_record`) on the receipt.

The link that follows ("How to stop re-keying claims") stays.

## Line 160: insurance, carrier-portal card (make conditional)

Current:

> Staff key a submission into several carrier portals, and each save is
> checked by opening it again in a fresh browser session.

Proposed:

> Staff key a submission into several carrier portals. Each step is checked on
> the current screen by default, or via a reopened view, separate read-only
> session, or API/export when available, with the check level stamped on the
> receipt.

## Line 176: insurance, record-check table (make conditional)

Current row:

| Your setup | Second read | Result OpenAdapt can give |
| --- | --- | --- |
| Broker entering into a carrier portal | The saved claim, opened again in a fresh browser session | Checked on screen, at the time of the run |

Proposed row:

| Your setup | Graded read-back | Result OpenAdapt can give |
| --- | --- | --- |
| Broker entering into a carrier portal | Bounded check on the current screen by default, or reopened record, separate read-only session, or API/export when available | Receipt stamps exact check level (`same_screen`, `reopened`, `separate_session`, or `system_of_record`) |

## Line 218: dental page description (remove)

Current:

> After a demonstration, OpenAdapt looks up coverage in a payer portal and
> carries the answer into your practice system. We measured the look-up on
> openIMIS, an open-source insurance system, with synthetic members.

The openIMIS result behind this sentence was checked by SQL on a test copy
with database access. A payer portal gives no such access, so the measured
check does not carry over to the setup the sentence describes.

Proposed: remove this text. The site's registry test
(`tests/siteRegistry.test.js:86`) requires every page to have a description of
240 characters or fewer, so removing it means either replacing it or removing
the page. If the page stays, a replacement that makes no check claim:

> After a demonstration, OpenAdapt looks up coverage in a payer portal and
> carries the answer into your practice system after a person approves it. We
> qualify the workflow on your own portal first.

The same claim appears in the page's answer (line 221), workflow steps (line
231) and test section (line 239). If the description goes, those lines need
the same decision.
