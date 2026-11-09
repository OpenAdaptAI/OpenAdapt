# Compliance and operator notes

This page is for people who deploy openadapt-privacy where recordings can hold
protected health information (PHI) or other personal data, and who need to
explain to a reviewer how the scrubbing is controlled. For installation and
usage, see the [README](../README.md).

Scrubbing is one control inside a reviewed process for data leaving your
environment. It does not guarantee that an artifact is free of protected data.

## Fail-closed behavior

The package raises an error instead of returning output that looks scrubbed
but isn't.

| Condition | Result |
| --- | --- |
| The spaCy model is not installed or not allowlisted (only `en_core_web_sm` is), the language is not `en`, or `SCRUB_CONFIG_TRF` names a different model | `PrivacyModelUnavailable` before any text is analyzed. The package never downloads a model at run time. |
| spaCy or a Presidio package is missing | `ImportError`, or `PrivacyModelUnavailable` when `validate_ready` finds a Presidio package missing. Nothing is scrubbed. |
| A built-in provider fails to import when you ask `ScrubbingProviderFactory` for one | `ScrubbingProviderUnavailable`, never an empty list. |
| A provider can't handle a modality the recording needs | `ScrubbingProviderUnavailable` from `validate_ready`, before `Recording.scrub` reads any content. |
| A screenshot names a file on disk but its pixels were never loaded | `UnscrubbedScreenshot`. A scrubbed `Screenshot` never keeps the `path` to the original file. |
| Code changes the scrub settings while `Recording.scrub` is running | `ScrubbingPolicyChanged`. Each recording is scrubbed under one snapshot of the settings. |

The CI scanner fails closed too: it exits `2` when it can't read the tree or
the tree is empty, and `--self-test` exits `1` if any rule does not fire on its
planted fixtures. Run `--self-test` in CI before the scan, so a rule that stops
matching fails the build.

## Provenance record

`Recording.scrub` adds this entry to the scrubbed recording's metadata. It
holds no scrubbed content.

```json
{
  "_openadapt_privacy": {
    "schema_version": 1,
    "provider": "PRESIDIO",
    "provider_class": "openadapt_privacy.providers.presidio.PresidioScrubbingProvider",
    "package_version": "<installed version>",
    "policy_sha256": "<SHA-256 of the complete settings, as sorted JSON>",
    "modalities": ["TEXT"],
    "status": "completed",
    "omitted_modalities": []
  }
}
```

`omitted_modalities` is `["PIL_IMAGE"]` when you call
`scrub(..., scrub_images=False)`, so a reviewer can see that screenshots were
left out on purpose. For other workflows, `ScrubbingProvider.evidence()`
returns the same fields except `omitted_modalities`.

## What the tests show

- `tests/test_phi_recall.py` has 27 synthetic identifiers: names, email
  addresses, phone numbers, an SSN, card numbers, a bank account number, dates
  of birth, street addresses, an IP address, a URL, medical record numbers, a
  member ID, and a provider license. The test requires all 27 to be redacted and
  pins the entity type each one must produce. Nine ordinary UI strings, such as
  `Click Save to continue.`, must come back unchanged.
- The data is synthetic, not clinical. No clinical validation is claimed.
- CI runs the suite on Python 3.10, 3.11, and 3.12 without Presidio, and the
  full suite with Presidio on Python 3.12.
- `openadapt-privacy-scan` matches regular expressions for the shapes it names.
  It does not claim clinical validation.

## Recommended review steps

1. Scrub a copy. Keep the original where it already is.
2. Check every output file. Re-run the text scrubber or the CI scanner on text
   output, and look at every scrubbed screenshot, because OCR misses some text.
3. Tie the human or policy approval to the checked artifact, for example by its
   hash, and not to the fact that the scrubber ran. A scrubber that finished
   without an error is not evidence that the artifact is clean.
4. Keep the provenance record with the artifact.

## Repository status

How OpenAdapt classifies and supports its repositories is described in the
organization's
[repository lifecycle](https://github.com/OpenAdaptAI/.github/blob/main/REPOSITORY_LIFECYCLE.md).
