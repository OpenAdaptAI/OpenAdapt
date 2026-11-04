# Setting up the write verifier with no person decisions: what the literature says

Written 2026-11-03. This review asks one question: can an agent produce all
of an app's verifier setup, with each piece checked by a machine instead of
by a person, without making the verifier easier to fool? The design that
follows from it is in [ZERO_TOUCH_DESIGN.md](ZERO_TOUCH_DESIGN.md).

Every source below was opened at a primary copy (the paper, its arXiv page,
the publisher's page, or the vendor's own documentation). Where only an
abstract was read, the entry says so. Sources that could not be opened are
listed at the end and are not used as evidence.

## The problem

Setting up the verifier for an app needs four kinds of per-app material:

- **Contracts:** what a correct write of each kind looks like. Which rows
  and columns change, to what, and what must not change.
- **Read access:** which stores the verifier reads, with which account.
- **Excuses:** writes the app makes on its own (a time stamp, a counter, a
  last-login row) that may change without failing the check.
- **Identity:** which row is "the record", and on screen, which region
  shows which record is open.

The contract generator (`docs/GENERATE_CONTRACTS.md`) already proposes
contracts from the schema and a few demonstration writes. It asks a person
what it can't decide. In two pre-registered retests on apps it had never
seen, that left too much to a person. Across the first retest's four apps,
setup needed 9 to 15 person decisions (the bar is 3). In the second, one app
took 64.5 minutes and 12 decisions (the bars are 60 minutes and 3), and
another reached the 120-minute limit with no usable setup. The decisions
were of a few recurring types:

- which changed columns are the app's own bookkeeping;
- how long the app may keep writing after a write;
- how a task's value is stored (a word stored as a number, a flag as text);
- where a field is stored when two places fit;
- which demonstrations to make;
- hand edits to generated contracts;
- approving the learned excuses.

Two facts frame the review. First, an excuse is exactly where a false
`CONFIRMED` comes from: in the first retest, the one fault that passed as
verified passed through a bookkeeping answer that excused a column on every
row of a table. Second, a person approving generated material is a weak
check. Polikarpova et al. found that about a third of the clauses an
invariant miner produced were wrong or irrelevant [A6]. So the target is
not "a person approves faster". It is "nothing is approved by a person, and
what can't be checked stays a stop".

## 1. Mining specifications from traces

**Likely invariants (Daikon).** Ernst et al. check a fixed grammar of
candidate properties (constants, ranges, linear relations, orderings)
against every recorded sample, and keep a property only if no sample
breaks it [A1]. A surviving property must also pass a chance test: it is
reported only if the probability that it held by accident is below a limit
the authors set at 0.01. Their companion paper adds two filters that
transfer directly [A2]. Compare only variables of compatible types, and
don't count a value that wasn't reassigned as fresh evidence. The tool
paper shows the trace format is neutral, so database row snapshots can be
fed in as samples [A3].

What it says about our data: with three to five demonstrations, almost no
property passes a chance test. A Daikon-style learner over a handful of
write diffs reports bounds and constants that hold by coincidence [A1].
Nimmer and Ernst say plainly that dynamic inference is unsound because it
depends on the samples, and pair it with a static checker to remove
properties that aren't universally true [A5]. Hellendoorn et al. label each
mined invariant by whether it survives held-out tests [A8]. That is a cheap,
mechanical form of the same idea: mine from some demonstrations, check on
others.

**Online learning and approximate rules.** DIDUCE starts with the
strictest hypothesis and relaxes it on each violation [A9]. Perracotta
keeps temporal rules that hold in a fraction of subtraces above a
threshold, and its property count is very sensitive to that threshold
[A10]. Texada reports support and confidence, so it can mine rules that
almost always hold [A11]. These methods are useful for proposing candidate
background writes. They must never accept an excuse: a rule with
confidence below 1, or one that widens on each violation, tolerates the
rare deviation the verifier exists to catch.

**Checking mined specifications by mutation.** SpecFuzzer generates
candidate assertions, drops those the tests falsify, then ranks the
survivors by how many code mutants they kill [A13]. Schuler, Dallmeier and
Zeller use mined invariants the other way round, as a signal of which
mutants matter [A14]. Both measure a specification by the faults it
catches. That is the gate this design needs.

**Invariants over database state.** Yang et al. found that Rails apps keep
most data constraints in app code, not in the database [A16]. The Coco
study extracted about 289 constraints per app from code and found that
most extra dependencies reported by data profilers were false positives
[A17]. The profilers treat time columns as structure, and their sampling
reports random columns as unique. A schema alone underspecifies a correct
write.

## 2. LLM-generated specifications and test oracles

**Postconditions from intent.** nl2postcond turns a natural-language
description into executable postconditions and scores them twice [B1]:
correctness (the reference code passes) and completeness (how many buggy
variants the postcondition fails). The best GPT-4 setting was correct for
158 of 164 problems, with mean bug-completeness 0.85. The trade-off is the
lesson. `true` is trivially correct. Type-check postconditions were the
most common kind (47.4%) but caught only 14% of natural bugs, against 82%
for arithmetic equalities. A simpler prompt raised correctness and cut
completeness by up to about 30%. Lahiri scores Dafny specifications the
same two ways, with the verifier perturbing outputs instead of needing
mutant programs [B2].

**Generate, check, repair.** SpecGen [B3] and AutoSpec [B4] loop an LLM
with a verifier until the specification verifies. Lemur formalizes the
loop so that a wrong LLM proposal can't produce a wrong verdict [B5].
Clover accepts generated code only when code, docstring and annotations
agree, and reports no false positives on adversarial instances [B6]. The
caution: "it verifies" rewards weakness. A repair loop driven only by
"this correct write was refuted" will loosen a contract until everything
passes, unless a drop in strength rejects the repair.

**The oracle problem.** Barr et al. sort oracles into specified, derived,
implicit and human [B8]. Contracts generated from demonstrations are
derived oracles: they can be no more correct than the demonstrations.
Konstantinou, Degiovanni and Papadakis found that LLM oracles mostly encode
what the code does, not what it should do [B12]. Hossain et al. re-ran a
published neural oracle generator at scale: more than 47% of its emitted
assertions were false positives, and the true ones added 0.3% fault
detection [B10]. Shin et al. found no correlation between how much a
generated oracle resembles a reference and how many faults it finds [B11].
So a generated contract is judged by behaviour (what it confirms and what
it refutes), never by how plausible it looks.

**Metamorphic relations.** Metamorphic testing checks relations between
several runs instead of expected values [B14, B15]. Rigger and Su found 175
database bugs by checking that a query's three-valued partitions add up to
the whole [B16]. Relations useful here: a write read back through a second
path agrees; a repeated "make sure it exists" write adds nothing; untouched
rows stay identical. Only 34% to 50% of LLM-proposed relations were valid
in a 37-system study (abstract only) [B17]. A relation may add a stop,
never a pass, and is enabled only if it holds on all clean traffic.

## 3. Validating generated oracles by planted faults

**Two-sided oracle assessment.** Jahangirova et al. assess an assertion
oracle on both sides [C5]. Generated correct runs expose false positives,
and mutants expose false negatives. A person refines the assertion and the
loop repeats. Oracles improved this way caught 48.6% more faults. The plan
here (replay correct writes, plant faults) is that method applied to
database writes, with the person removed. An excuse is an oracle weakening.
It is justified only by a false positive it removes, and it is exposed only
when a planted fault lands inside it.

**What a full kill rate does and does not mean.** Just et al. found mutant
detection correlated with real-fault detection on 357 real faults, but 27%
of real faults were not coupled to any mutant the triggering tests caught,
and 17% matched no mutation operator [C3]. Once suite size is controlled,
the correlation between mutation score and real-fault detection is weak
(abstract only) [C4]. The Papadakis survey shows that redundant and
subsumed mutants inflate a single score, and that equivalent-mutant
detection is undecidable in general [C2]. So catching every planted fault is
necessary for admission, not proof of correctness. Report results per fault
class and next to the false-stop rate.

**Faults for data systems.** SQLMutation's operators cover wrong columns
and constants, joins and NULL handling, and only 5.8% of its mutants were
equivalent [C9]. Elle checks histories for duplicate writes, lost updates
and real-time-order violations, and found a default retry in one database
that silently re-applied writes [C10]. Elle makes every write traceable by
using unique values, which makes "which write made this row" decidable.

**LLM-made faults.** LLM mutants caught more real bugs than rule-based ones
(76.5% against 44.2% in the latest version of Wang et al.), with more
duplicates, more mutants that don't build, and more equivalent ones [C12].
Meta's mutation-guided test generator found 25% of its LLM mutants
trivially equivalent, and its LLM equivalence judge had recall 0.47 before
preprocessing [C13]. Naively prompted bug injectors collapse into one-line
edits, and self-play pipelines add a check that every part of a fault is
needed [C14]. CodeT [C15] and CURE [C16] show that agreement between
artefacts from one model is consensus, not ground truth; CURE works because
fixed ground-truth tests anchor it.

**Independence.** Knight and Leveson ran 27 independently written versions
of one program on a million inputs and rejected the independence of their
failures at 99% confidence (read from a course copy) [B19]. Kim et al.
found that when two LLMs are both wrong they give the same wrong answer 60%
of the time on one leaderboard, and that stronger models are more
correlated across providers [B20]. LLM judges favour their own outputs
[B21]. So a different model for the fault generator is not enough.
Independence has to come from a different method: a rule-based fault floor
that no model writes, faults derived from the recorded write rather than
from the contract, and faults written by someone else.

**Holding faults out.** A generator that iterates against a fault suite will
fit it. Thresholdout answers adaptive queries against a holdout with
provable validity, spending an overfitting budget when it reveals a value
[C18]. The Ladder reports a new score only on a clear improvement [C19].
Recht et al. rebuilt two test sets and saw accuracy drop 3% to 15% [C20].
For this design: keep a hidden fault set that answers only pass or fail,
and regenerate the visible suite from time to time.

## 4. Change capture and schema understanding

**Change data capture.** Debezium streams each row change with its before
and after images, its transaction id and its order in the transaction [A18].
A table's before image holds only the key unless its replica identity is
full. A trigger-based audit table records the session user, the
application name and the changed fields [A20]. Transaction ids separate the
agent's commit from a background job in another transaction, which is the
strongest mechanical signal for "this row is bookkeeping". It fails when
the app writes its bookkeeping in the same transaction.

**Bookkeeping columns.** No reviewed work infers bookkeeping columns
directly. The recipe that follows from the sources: a column is a candidate
when it changes outside the agent's transaction, or on writes of every
kind whatever the task's values; it is excused only with a value rule (the
current time, a step of one, the acting user), and only after planted
faults in it are still caught.

**Keys and foreign keys.** Rostin et al. classify inclusion dependencies as
foreign keys with features such as name similarity and out-of-range share
(F-measure above 0.8) [A21]. Their errors come from small-integer columns
overlapping the ids of small tables. Zhang et al. show auto-increment
ranges creating false inclusions and add a randomness test [A22]. Binder
[A23] and Faida [A24] find inclusion dependencies exactly or approximately,
and HyUCC finds unique column combinations [A25]. All of these propose
identity; none can accept it. A uniqueness that holds in today's rows is
not a key, and a coincidental join is how a contract binds the wrong record.

**Column roles.** Sherlock [A27], Sato [A28], Doduo [A29] and zero-shot
ChatGPT annotation [A30] label columns with semantic types at weighted F1
around 0.85 to 0.93 on web tables. None is trained on operational roles
(update time, version counter, acting user), and rare classes score much
worse (Sato's macro F1 is 0.735). A role label is a nomination. A column
named like a time stamp may hold a date that matters clinically.

**Provenance.** Why-provenance and where-provenance [A31], generalized as
semirings [A32], give the vocabulary for evidence: a `CONFIRMED` names the
row and column that hold the value, and the write that put it there.

## 5. GUI element and record identity

**Locators.** Similo scores elements on 14 weighted attributes and failed
to locate 11% of 801 targets, against 27% for the best multi-locator
baseline [D1]. Its authors note that without a threshold it returns a
matching but wrong element when the target is missing. Contextual clues
locate an element by the visible labels near it [D5]. Across records, an
identifying field is the one whose label stays fixed while its value
changes. VISTA reads each element through the DOM and through pixels and
hands any disagreement to a person [D6]. Over RDP or Citrix only pixels
exist, so the second reading has to be a read-back from the system of
record.

**Accessibility identity.** UIA's `AutomationId` should be the same in every
instance of an app, but it is optional and not guaranteed stable across
builds; `Name` should match the visible label and is not a unique key
[D9]. Many thick clients expose no `AutomationId`, so the descriptor must be
validated per app.

**Grounding models.** OmniParser [D10], ScreenAI [D11] and SeeClick [D12]
find UI elements in screenshots. On professional high-resolution apps the
best model reached 18.9% [D14]. A model may propose a region; it can't be
the gate.

**Self-healing tools.** Healenium heals to the best candidate above a score
cap (0.5 in its example config) [D16]. Testim improves a locator when its
score drops below 70% [D17]. UiPath's Healing Agent picks a selector by
vote and can be limited to recommendations [D18]. Every one of them heals to
keep a test running. For a write gate the cost is reversed: a wrong heal
that keeps a run going is worse than a stop.

**Record identity in the store.** FHIR's `Patient/$match` with
`onlyCertainMatches` returns nothing when the match is ambiguous [D15]. That
is the right shape for an identity check: one certain match, or a stop.

## 6. How commercial tools onboard an app

| Tool | Generated automatically | A person still decides | How it validates |
| --- | --- | --- | --- |
| UiPath [D18, D21, D22, D23] | Element descriptors, anchors, fallback targets; a healed selector by vote | Which element to indicate; heal or only recommend; document fields in Validation Station | "Verify execution": an element appears, disappears or changes text |
| Power Automate desktop [D24, D25] | Selectors from a Ctrl+click | Which element; selector order | "Test selector": success, failure, or several matches (the top-left one wins at run time) |
| Blue Prism [D26] | Every attribute of the spied element | Which attributes count, how each matches, which are dynamic | Attribute match at run time |
| Automation Anywhere [D27] | Default search criteria | Removes volatile criteria | Separate validation properties after the element is found |
| Anthropic and OpenAI computer use [D28, D29] | Actions from screenshots | Confirms consequential actions | A screenshot after each step; check the outcome in the app |
| Airbyte [D30] | The discovered schema and its changes | Approves non-breaking changes; fixes breaking ones | A removed key or cursor pauses the connection |

Every tool keeps a person on the choice of element or record identity, and
none checks a write against the system of record. "Verify execution"
checks that the screen reacted.

## Which ideas transfer

1. **Admit by planted faults, on both sides.** A generated contract is
   admitted only if it confirms every held-out correct write and stops
   every planted fault [C5, A13, B1]. Score correctness and strength
   separately, per fault class, next to the false-stop rate.
2. **No excuse without a value rule, and plant faults inside every
   excuse.** An unchecked cell is a weakening. Replace "any value" with a
   rule the read can check (the current time, the acting user, a copy of
   another column), and plant a fault in that cell. If no rule fits, the
   kind isn't admitted [C5, C2].
3. **Faults from the recorded write, not from the contract.** The fault
   planter reads the held-out write's diff, the schema and the task's
   values. It never reads the contract. Add LLM-written faults only on top
   of a rule-based floor, and faults written by someone else as a hidden
   holdout [B19, B20, C12, C18].
4. **Hold demonstrations out.** Mine from some demonstrations; validate on
   others the generator never saw [A8].
5. **Exact rules only; approximate mining proposes.** Confidence-below-1
   rules, relax-on-violation learners and role labels can nominate an
   excuse or an identity rule. Only the sandbox can accept it [A9, A10,
   A27–A30].
6. **No model in the gate.** A model may propose. A deterministic check
   decides, and a model never judges its own output [B5, B21, B11].
7. **Fail closed where it can't be checked.** What the sandbox can't
   validate stays `UNCERTAIN` or stops, and is listed for a person to look
   at, never excused [D1, D15].

## Risks

- **An autogenerated excuse is where a false `CONFIRMED` comes from.** The
  first retest's only fault that passed as verified went through one. The
  defence is item 2 above: an excused cell has a value rule, and a fault
  planted in it must be caught.
- **Shared blind spots.** If the fault planter and the contract generator
  share an idea of which columns matter, a full kill rate means nothing.
  The planter must not read contracts, and its operators must cover every
  column the write touched and rows it didn't touch.
- **Admission is necessary, not sufficient.** 17% to 27% of real faults
  aren't coupled to synthetic ones [C3]. Product copy must not present an
  admitted contract as proof.
- **The sandbox isn't production.** Look-alike records, other users and
  background jobs may be rarer in a sandbox copy than in a live store
  [C10]. The live verifier keeps its whole-database frame and recheck.
- **Overfitting the gate.** A generator that sees which faults survived can
  patch case by case. Keep a hidden fault set and regenerate the visible
  one [C18, C19].
- **Few demonstrations.** With three to five writes, coincidences pass as
  rules [A1]. Held-out writes and planted faults catch some of them;
  demonstrations chosen to differ catch more.

## Sources

Opened at a primary copy unless marked.

**A. Mining and databases**

- [A1] Ernst, Cockrell, Griswold, Notkin. "Dynamically discovering likely program invariants to support program evolution." IEEE TSE 27(2), 2001. https://homes.cs.washington.edu/~mernst/pubs/invariants-tse2001.pdf
- [A2] Ernst, Czeisler, Griswold, Notkin. "Quickly detecting relevant program invariants." ICSE 2000. https://homes.cs.washington.edu/~mernst/pubs/invariants-relevance-icse2000.pdf
- [A3] Ernst et al. "The Daikon system for dynamic detection of likely invariants." Science of Computer Programming 69, 2007 (abstract page). https://people.csail.mit.edu/cpacheco/publications/daikon-tool-scp2006-abstract.html
- [A5] Nimmer, Ernst. "Invariant inference for static checking: an empirical evaluation." FSE 2002. https://homes.cs.washington.edu/~mernst/pubs/annotation-study-fse2002.pdf
- [A6] Polikarpova, Ciupa, Meyer. "A comparative study of programmer-written and automatically inferred contracts." ISSTA 2009 (abstract page). https://plse.cs.washington.edu/daikon/pubs/PolikarpovaCM2009-abstract.html
- [A8] Hellendoorn, Devanbu, Polozov, Marron. "Are my invariants valid? A learning approach." arXiv:1903.06089, 2019. https://www.microsoft.com/en-us/research/?p=605031
- [A9] Hangal, Lam. "Tracking down software bugs using automatic anomaly detection" (DIDUCE). ICSE 2002. https://cs.fit.edu/~pkc/id/related/hangal-icse02.pdf
- [A10] Yang, Evans, Bhardwaj, Bhat, Das. "Perracotta: mining temporal API rules from imperfect traces." ICSE 2006. https://www.cs.virginia.edu/~evans/pubs/perracotta-packaged.pdf
- [A11] Lemieux, Beschastnikh. "Investigating program behavior using the Texada LTL specifications miner." ASE 2015 tool demo. https://www.cs.ubc.ca/~bestchai/papers/ase15-texada-tool-demo.pdf
- [A13] Molina, d'Amorim, Aguirre. "Fuzzing class specifications" (SpecFuzzer). arXiv:2201.10874. https://arxiv.org/abs/2201.10874
- [A14] Schuler, Dallmeier, Zeller. "Efficient mutation testing by checking invariant violations." ISSTA 2009 (abstract page). https://plse.cs.washington.edu/daikon/pubs/SchulerDZ2009-abstract.html
- [A16] Yang, Sethi, Yan, Cheung, Lu. "Managing data constraints in database-backed web applications." ICSE 2020 (program page). https://2020.icse-conferences.org/details/icse-2020-papers/85/Managing-data-constraints-in-database-backed-web-applications
- [A17] Liu et al. "Leveraging application data constraints to optimize database-backed web applications" (Coco). PVLDB 16(6), 2023. https://www.vldb.org/pvldb/vol16/p1208-liu.pdf
- [A18] Debezium. "PostgreSQL connector" documentation. https://debezium.io/documentation/reference/stable/connectors/postgresql.html
- [A20] PostgreSQL wiki. "Audit trigger 91plus." https://wiki.postgresql.org/wiki/Audit_trigger_91plus
- [A21] Rostin, Albrecht, Bauckmann, Naumann, Leser. "A machine learning approach to foreign key discovery." WebDB 2009. https://hpi.de/oldsite/fileadmin/user_upload/fachgebiete/naumann/publications/2009/WebDB09_crc.pdf
- [A22] Zhang, Hadjieleftheriou, Ooi, Procopiuc, Srivastava. "On multi-column foreign key discovery." PVLDB 3(1), 2010. https://vldb.org/pvldb/vol3/R72.pdf
- [A23] Papenbrock, Kruse, Quiané-Ruiz, Naumann. "Divide & conquer-based inclusion dependency discovery" (Binder). PVLDB 8(7), 2015. https://hpi.de/oldsite/fileadmin/user_upload/fachgebiete/naumann/publications/PDFs/2015_papenbrock_divide.pdf
- [A24] Kruse et al. "Fast approximate discovery of inclusion dependencies" (Faida). BTW 2017. https://dl.gi.de/items/5a813877-a923-4236-95cc-d08ba2c9752a/full
- [A25] Papenbrock, Naumann. "A hybrid approach for efficient unique column combination discovery" (HyUCC). BTW 2017. https://dl.gi.de/items/f9fc2a40-8c8a-4250-b92f-228c0b26807d/full
- [A27] Hulsebos et al. "Sherlock: a deep learning approach to semantic data type detection." KDD 2019. https://arxiv.org/abs/1905.10688
- [A28] Zhang et al. "Sato: contextual semantic type detection in tables." PVLDB 2020. https://arxiv.org/abs/1911.06311
- [A29] Suhara et al. "Annotating columns with pre-trained language models" (Doduo). SIGMOD 2022. https://arxiv.org/abs/2104.01785
- [A30] Korini, Bizer. "Column type annotation using ChatGPT." arXiv:2306.00745, 2023. https://arxiv.org/abs/2306.00745
- [A31] Buneman, Khanna, Tan. "Why and where: a characterization of data provenance." ICDT 2001. http://homepages.inf.ed.ac.uk/opb/papers/ICDT2001.pdf
- [A32] Green, Karvounarakis, Tannen. "Provenance semirings." PODS 2007. https://www.cs.ucdavis.edu/~green/papers/pods07.pdf

**B. LLM specifications, oracles, independence**

- [B1] Endres, Fakhoury, Chakraborty, Lahiri. "Can large language models transform natural language intent into formal method postconditions?" FSE 2024. https://arxiv.org/abs/2310.01831
- [B2] Lahiri. "Evaluating LLM-driven user-intent formalization for verification-aware languages." FMCAD 2024 (abstract). https://arxiv.org/abs/2406.09757
- [B3] Ma, Liu, Li, Xie, Bu. "SpecGen: automated generation of formal program specifications via large language models." ICSE 2025. https://arxiv.org/abs/2401.08807
- [B4] Wen et al. "Enchanting program specification synthesis by large language models using static analysis and program verification" (AutoSpec). arXiv:2404.00762. https://arxiv.org/abs/2404.00762
- [B5] Wu, Barrett, Narodytska. "Lemur: integrating large language models in automated program verification." ICLR 2024. https://arxiv.org/abs/2310.04870
- [B6] Sun, Sheng, Padon, Barrett. "Clover: closed-loop verifiable code generation." arXiv:2310.17807. https://arxiv.org/abs/2310.17807
- [B8] Barr, Harman, McMinn, Shahbaz, Yoo. "The oracle problem in software testing: a survey." IEEE TSE 41(5), 2015. https://web.eecs.umich.edu/~weimerw/2018-481/readings/testoracles.pdf
- [B10] Hossain, Filieri, Dwyer, Elbaum, Visser. "Neural-based test oracle generation: a large-scale evaluation and lessons learned." ESEC/FSE 2023. https://arxiv.org/abs/2307.16023
- [B11] Shin, Hemmati, Wei, Wang. "Assessing evaluation metrics for neural test oracle generation." IEEE TSE 50(9), 2024. https://arxiv.org/abs/2310.07856
- [B12] Konstantinou, Degiovanni, Papadakis. "Do LLMs generate test oracles that capture the actual or the expected program behaviour?" arXiv:2410.21136. https://arxiv.org/abs/2410.21136
- [B14] Segura, Fraser, Sanchez, Ruiz-Cortés. "A survey on metamorphic testing." IEEE TSE 42(9), 2016. https://eprints.whiterose.ac.uk/110335/1/segura16-tse.pdf
- [B15] Chen et al. "Metamorphic testing: a review of challenges and opportunities." ACM CSUR 51(1), 2018. https://i.cs.hku.hk/~tse/Papers/2010s/hlmtCSUR.html
- [B16] Rigger, Su. "Finding bugs in database systems via query partitioning." OOPSLA 2020 (program page). https://2020.splashcon.org/details/splash-2020-oopsla/87/Finding-Bugs-in-Database-Systems-via-Query-Partitioning
- [B17] Zhang, Sun, Dong, Liu. "Can large language models discover metamorphic relations? A large-scale empirical study." SANER 2025 (abstract page only). https://conf.researchr.org/details/saner-2025/saner-2025-papers/41/Can-Large-Language-Models-Discover-Metamorphic-Relations-A-Large-Scale-Empirical-Stu
- [B19] Knight, Leveson. "An experimental evaluation of the assumption of independence in multiversion programming." IEEE TSE 12(1), 1986 (course copy, not the IEEE version). https://www.kth.se/social/files/564df871f2765419e306178d/KnightLeveson.pdf
- [B20] Kim, Garg, Peng, Garg. "Correlated errors in large language models." ICML 2025. https://arxiv.org/abs/2506.07962
- [B21] Panickssery, Bowman, Feng. "LLM evaluators recognize and favor their own generations." arXiv:2404.13076. https://arxiv.org/abs/2404.13076

**C. Mutation, fault injection, holdouts**

- [C2] Papadakis, Kintis, Zhang, Jia, Le Traon, Harman. "Mutation testing advances: an analysis and survey." Advances in Computers 112, 2019. https://discovery.ucl.ac.uk/id/eprint/10056704/1/survey.pdf
- [C3] Just, Jalali, Inozemtseva, Ernst, Holmes, Fraser. "Are mutants a valid substitute for real faults in software testing?" FSE 2014. https://homes.cs.washington.edu/~rjust/publ/mutants_real_faults_fse_2014.pdf
- [C4] Papadakis, Shin, Yoo, Bae. "Are mutation scores correlated with real fault detection?" ICSE 2018 (abstract page only). https://conf.researchr.org/details/icse-2018/icse-2018-Technical-Papers/78/
- [C5] Jahangirova, Clark, Harman, Tonella. "Test oracle assessment and improvement." ISSTA 2016. https://discovery.ucl.ac.uk/1493269/1/main.pdf
- [C9] Tuya, Suárez-Cabal, de la Riva. "Mutating database queries." IST 49(4), 2007. https://digibuo.uniovi.es/dspace/bitstream/10651/29255/2/ist-2006-sqlmutation.pdf
- [C10] Kingsbury, Alvaro. "Elle: inferring isolation anomalies from experimental observations." PVLDB 14(3), 2021. https://arxiv.org/abs/2003.10554
- [C12] Wang et al. "A comprehensive study on large language models for mutation testing." arXiv:2406.09843 (v5). https://arxiv.org/abs/2406.09843
- [C13] Foster et al. "Mutation-guided LLM-based test generation at Meta." arXiv:2501.12862. https://arxiv.org/abs/2501.12862
- [C14] Wei et al. "Toward training superintelligent software agents through self-play SWE-RL." arXiv:2512.18552. https://arxiv.org/abs/2512.18552
- [C15] Chen et al. "CodeT: code generation with generated tests." arXiv:2207.10397. https://arxiv.org/abs/2207.10397
- [C16] Wang, Yang, Tian, Shen, Wang. "Co-evolving LLM coder and unit tester via reinforcement learning" (CURE). arXiv:2506.03136. https://arxiv.org/abs/2506.03136
- [C18] Dwork, Feldman, Hardt, Pitassi, Reingold, Roth. "Generalization in adaptive data analysis and holdout reuse." arXiv:1506.02629. https://arxiv.org/abs/1506.02629
- [C19] Blum, Hardt. "The Ladder: a reliable leaderboard for machine learning competitions." arXiv:1502.04585. https://arxiv.org/abs/1502.04585
- [C20] Recht, Roelofs, Schmidt, Shankar. "Do ImageNet classifiers generalize to ImageNet?" arXiv:1902.10811. https://arxiv.org/abs/1902.10811

**D. GUI identity and commercial tools**

- [D1] Nass, Alégroth, Feldt, Leotta, Ricca. "Similarity-based web element localization for robust test automation." ACM TOSEM 32(3), 2023. https://arxiv.org/abs/2208.00677
- [D5] Yandrapally, Thummalapenta, Sinha, Chandra. "Robust test automation using contextual clues." ISSTA 2014. https://www.microsoft.com/en-us/research/?p=167470
- [D6] Stocco, Yandrapally, Mesbah. "Vista: web test repair using computer vision." ESEC/FSE 2018 demo. https://people.ece.ubc.ca/~astocco/pubs/2018-Stocco-FSE18-demo.pdf
- [D9] Microsoft. "Automation element property identifiers." https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-automation-element-propids
- [D10] Lu, Yang, Shen, Awadallah. "OmniParser for pure vision based GUI agent." arXiv:2408.00203. https://arxiv.org/abs/2408.00203
- [D11] Baechler et al. "ScreenAI: a vision-language model for UI and infographics understanding." IJCAI 2024. https://arxiv.org/abs/2402.04615
- [D12] Cheng et al. "SeeClick: harnessing GUI grounding for advanced visual GUI agents." arXiv:2401.10935. https://arxiv.org/abs/2401.10935
- [D14] Li et al. "ScreenSpot-Pro: GUI grounding for professional high-resolution computer use." arXiv:2504.07981. https://arxiv.org/abs/2504.07981
- [D15] HL7. "Patient-match operation", FHIR R4. https://hl7.org/fhir/R4/patient-operation-match.html
- [D16] Healenium. "How Healenium works" and the healenium-web README. https://healenium.io/docs/how_healenium_works
- [D17] Tricentis. "Locators: auto improve" (Testim). https://docs.tricentis.com/testim/content/test-management/locators-auto-improve.htm
- [D18] UiPath. "What is Healing Agent?" https://docs.uipath.com/agents/automation-cloud/latest/user-guide-ha/what-is-healing-agent
- [D21] UiPath. "About Object Repository." https://docs.uipath.com/studio/standalone/2023.10/user-guide/about-object-repository.md
- [D22] UiPath. "Click" activity. https://docs.uipath.com/activities/other/latest/ui-automation/n-click
- [D23] UiPath. "Validation Station." https://docs.uipath.com/document-understanding/automation-cloud/latest/classic-USER-GUIDE/validation-station
- [D24] Microsoft. "Automate using UI elements" (Power Automate desktop). https://learn.microsoft.com/en-us/power-automate/desktop-flows/ui-elements
- [D25] Microsoft. "Test a selector" (Power Automate desktop). https://learn.microsoft.com/en-us/power-automate/desktop-flows/test-selectors
- [D26] Blue Prism. "Application Modeller" (v7.0). https://documentation.blueprism.com/bp-7-0/en-us/frmIntegrationAssistant.htm
- [D27] Automation Anywhere. "Search criteria (Recorder package)." http://docs.automationanywhere.com/r/automation-360/search-criteria-the-recorder-package
- [D28] Anthropic. "Computer use tool." https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/computer-use-tool
- [D29] OpenAI. "Computer use" guide. https://developers.openai.com/api/docs/guides/tools-computer-use
- [D30] Airbyte. "Schema change management." https://docs.airbyte.com/platform/using-airbyte/schema-change-management

**Tried and not opened (not used as evidence):** Ammons, Bodik, Larus,
"Mining specifications" (POPL 2002); Staats et al., ISSTA 2012, on how well
people judge generated invariants; Cobb et al., WODA 2011, on invariants
over databases; the Metanome demo text; Schuler and Zeller, "checked
coverage" (ICST 2011); the Science version of the reusable holdout;
ROBULA+ and Leotta's multi-locator paper; WATER; mabl's documentation; the
full ICST 2016 paper on why record/replay tests break.
