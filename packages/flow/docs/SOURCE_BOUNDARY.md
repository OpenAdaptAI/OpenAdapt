# Source and evaluation boundary

OpenAdapt is open-core. The compiler, governed runtime, safety mechanisms,
public interfaces, reusable evaluation harnesses, and fake-patient reference
workflows are public. Public evidence may include permitted raw benchmark files
or bounded aggregate summaries.

High-leverage evaluation data remains private: grown deployment failure
corpora, tuned adversary parameters, deployment-derived thresholds, per-system
effect-oracle recipes, real-EMR-tied datasets, and raw private evaluation rows.
The public package release guard enforces this boundary in both the source tree
and built archives.

## Previously published material

Material already published in Git history is public reference material. Moving
a copy to a private repository does not make the historical version secret.
The private boundary applies to successor corpora, new examples, derived
tuning, deployment evidence, and recipes accumulated after the carve. Public
historical snapshots should not be described as confidential or relied on as a
future moat.

## Adding a public artifact

Data, evidence, static assets, models, and configuration files are admitted by
an exact path-and-SHA-256 inventory in `public-artifacts.json`. When a reviewed
public artifact is intentionally added or changed, regenerate the candidate:

```bash
python scripts/check_release_consistency.py --write-public-artifact-inventory
```

Review the entire manifest diff before committing it. Normal validation never
rewrites the inventory. Release checks compare the source tree, wheel, and
source distribution to the approved paths and hashes and reject unregistered,
renamed, or modified payloads.

Each wheel and source distribution embeds the manifest that belongs to its own
source ref. Recovery publication of an older reviewed ref validates its files
against that embedded historical manifest under the current schema and policy;
it must not compare old payload hashes with a newer checkout's manifest. A
wheel/sdist pair must embed identical manifests.

This control supplements code review; it does not decide whether an artifact is
safe to publish. Reviewers must still verify its provenance, license, data
classification, and source-availability policy.

The inventory covers payload-style files (data, evidence, static assets,
models, and deployment-shaped configuration). It is not general DLP and does
not claim to classify arbitrary Python, Markdown, or TeX source; those remain
subject to code review plus the path-token and provenance-signature guards.
