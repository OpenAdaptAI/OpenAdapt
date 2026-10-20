# Named sections

Every compiled bundle has a `sections.json` file. It groups the program's steps
into contiguous, named sections, such as "Who: title, first name, last name",
"Contact: street, city, state, postal code" and "Create New Patient". A program
map folds each section into one card: Desktop's review screen, the
`@openadapt/program-graph` package, and the program map on openadapt.ai.

The compiler finds the sections without a model. The same recording always
gives the same sections.

## Where the boundaries come from

The compiler uses these sources in priority order:

1. **The consequential write.** A save section starts at the step after the
   last input and runs through the write's confirmation, such as a confirm
   dialog that carries the record checks, or a READ or WAIT step that follows
   the write. The save section is always a section of its own.
2. **The recorded form structure.** The browser recorders note the part of
   the form each clicked target sits in (`section_capture.py`). That is the
   nearest fieldset legend, accordion or tab header, dialog, labelled
   landmark, or heading. A click that opens a section header starts a section
   named after that header. A bundle recorded before this capture still has
   the header click itself, so a named tab or button clicked between fields
   still starts a section.
3. **The fields.** All the steps for one field stay together: the focusing
   click, Ctrl+A, the typed value, and the Enter or picked option that
   finishes it. A section with more than four fields splits where the kind of
   field changes. The kinds are name, birth, address, and phone and email,
   the WHATWG autofill field groups.
4. **Narration**, when the recording has it. A sentence the person said
   names a section that has no title. It also splits a long section that the
   fields could not split.

Titles come from the interface's own words. When no title is found, the
title lists the fields, for example "3 fields: title, first name, last name".
A title never contains a recorded value or a run of four digits. For example,
a heading such as "Patient: Jane Doe" is not used.

## Display metadata only

The replayer, the program interpreter and the outcome classifier never read
`sections.json`. A test checks which modules import the sections code.

The file is outside the bundle's content digest and outside the workflow
contract hash. Here is why:

- A section changes how a program is shown, never what it does. Renaming a
  section during review must not invalidate a certification or a governed
  authorization. Both are bound to those two hashes.
- The file binds itself to one program instead. It records the workflow
  contract hash it was made for. When that hash no longer matches, or the
  sections break a rule (for example, a step in no section, or a write
  outside a save section), the sections are recomputed from the program, the
  stored form structure and the stored review edits. A changed file can
  rename a section, but it can't hide a step or move a write out of the save
  section.

For a sealed bundle, `sections.json` is encrypted like the other compiler
sidecars ([phi_at_rest.md](phi_at_rest.md)).

## Review

A person can rename, merge or split sections in Desktop's review screen, or
with `openadapt-flow sections`:

```bash
openadapt-flow sections BUNDLE
openadapt-flow sections BUNDLE --rename fields-step_000 "Patient name"
openadapt-flow sections BUNDLE --merge fields-step_000 fields-step_009
openadapt-flow sections BUNDLE --split fields-step_015 step_020
openadapt-flow sections BUNDLE --reset
```

Two edits are refused, in Desktop and in the command: merging the save
section with a field section, and splitting the save section. The edits are
stored beside the compiled sections and replayed when the program changes.

`--recording DIR` reads the form structure from a recording of the same
demonstration. It applies only to steps whose target matches (the same
selector, or the same role and name). This is how the certified OpenEMR
referral bundle, recorded before the capture existed, gets the "Who" and
"Contact" titles in `packages/program-graph/fixtures/openemr-referral`.

## In the program graph

`openadapt-flow visualize --format json` emits the sections as
`spec.sections`. Each section lists its node ids, the bundle's section id and
step ids, a title, and a subtitle such as "6 steps · 3 fields". The public
projection keeps the node ids and the kind. It replaces each title with a
fixed phrase and drops the field names.
