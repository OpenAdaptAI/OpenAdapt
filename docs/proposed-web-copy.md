# Proposed website copy: check scope without database access

Status: draft for approval. Nothing here is applied to the website.

These changes bring the insurance and dental pages in line with the repository
docs ([LIMITS.md](../packages/flow/docs/LIMITS.md#what-the-guarantee-covers-without-database-access),
[THREAT_MODEL.md](../packages/flow/docs/THREAT_MODEL.md#what-layer-1-covers)).
When a portal's own screens are the only independent read (no database, API,
log or export), the strongest check is a separately signed-in session. It
covers the target record as shown at the time of the read, and only where the
portal allows a second session and that read loads the record fresh from the
server.

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
> carrier portals. Where the portal allows a second signed-in session, the
> check opens the saved claim there and compares it field by field with what
> was entered. That shows the claim as the portal displayed it at the time of
> the check, not what the carrier does with it later. Where the portal allows
> no second session, a person reviews each save. The check is set up along
> with the workflow.

The link that follows ("How to stop re-keying claims") stays.

## Line 160: insurance, carrier-portal card (make conditional)

Current:

> Staff key a submission into several carrier portals, and each save is
> checked by opening it again in a fresh browser session.

Proposed:

> Staff key a submission into several carrier portals. Where a portal allows
> a second signed-in session, each save is checked by opening it again there.

## Line 176: insurance, record-check table (make conditional)

Current row:

| Your setup | Second read | Result OpenAdapt can give |
| --- | --- | --- |
| Broker entering into a carrier portal | The saved claim, opened again in a fresh browser session | Checked on screen, at the time of the run |

Proposed row:

| Your setup | Second read | Result OpenAdapt can give |
| --- | --- | --- |
| Broker entering into a carrier portal | The saved claim, opened again in a separate signed-in session, where the portal allows one and the second read loads the record fresh from the server | Checked in a separate session, as of the time of the check. With no second session, a person reviews |

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
