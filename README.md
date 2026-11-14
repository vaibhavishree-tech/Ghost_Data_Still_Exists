# Ghost Data Still Exists: deletion-audit agent

For each deletion request, the agent audits what the evidence shows at the request's `audit_at` time and writes `case_id, deletion_status, steps_json`. Statuses are `COMPLETE`, `INCOMPLETE` and `NOT_APPLICABLE`. The step list follows the schema and ordering in the problem statement.

## Quick start

```
python code/main.py --sample                 # audit the 32 sample requests and score them against sample_output.csv
python code/main.py                          # audit all 518 requests, write output.csv at the repository root, validate it
python code/evaluation/custom_cases.py       # run the three additional evaluation cases
```

Options for `main.py`:
- `--order natural|lexicographic`: row order of `output.csv` (default: numeric case order, which equals file order).
- `--no-ocr`: skip screenshot OCR (the screenshot cases then lose their evidence).

Layout the code expects:

```
<repo>/dataset/        the challenge files (not included in code.zip)
<repo>/code/           this solution
<repo>/work/           generated reports and caches
<repo>/output.csv      written by python code/main.py
```

Set `GHOST_DATASET_DIR` to use a dataset folder somewhere else.

## Requirements

- Python 3.13 was used for development. The code uses `from __future__ import annotations`, so recent Python 3 versions should work, but only 3.13 was tested.
- `tesseract` (OCR for screenshots) and `pdftotext` from poppler (PDF text). Override their locations with `TESSERACT_CMD` and `PDFTOTEXT_CMD`. If either is missing, the affected files are marked unavailable and the run continues; those cases may then be wrong.
- Python packages: install with `pip install -r requirements.txt` (only `pypdf`; everything else is the standard library).

## Repository layout

```
.
├── README.md
├── problem_statement   (Official problem statement)
├── requirements.txt
├── output.csv                  (written by code/main.py)
├── evaluation/
│   ├── evaluation_report.md
│   └── usage_report.md
└── code/
    ├── main.py
    ├── phase1_normalize.py
    ├── ghostdata/              (the audit pipeline)
    │   ├── config.py
    │   ├── io_utils.py
    │   ├── models.py
    │   ├── extractors.py
    │   ├── normalize.py
    │   ├── parsers.py
    │   ├── store.py
    │   ├── validate.py
    │   ├── identity.py
    │   ├── policy.py
    │   ├── linker.py
    │   ├── pipeline.py
    │   └── steps.py
    └── evaluation/             (evaluation code)
        ├── evaluate.py
        ├── validate_output.py
        └── custom_cases.py
```

### Top level

| File | Role |
|---|---|
| `README.md` | Setup, how the system works, design notes. |
| `requirements.txt` | Python dependencies (only `pypdf`). |
| `output.csv` | Answers for the 518 requests, written by `code/main.py`. |
| `evaluation/evaluation_report.md` | Accuracy numbers, additional cases, failures found and fixes. |
| `evaluation/usage_report.md` | Model usage of the final run (none: 0 calls, 0 tokens, $0). |

### `code/`

| File | Role |
|---|---|
| `main.py` | Entry point: audits the requests, writes `output.csv` and scores the sample cases. |
| `phase1_normalize.py` | Optional: runs only the evidence normalisation and writes a Phase 1 report. |

### `code/ghostdata/` (the audit pipeline, one module per job)

| Module | Role |
|---|---|
| `config.py` | Paths, external tool commands, resource-id prefixes and categories. |
| `io_utils.py` | CSV loading and timestamp parsing/formatting shared by all modules. |
| `models.py` | Data classes for evidence files, normalised records and activity events. |
| `extractors.py` | Text extraction from PDFs (`pdftotext` / `pypdf`) and screenshots (`tesseract` OCR). |
| `normalize.py` | Turns raw fields into one record shape: system, category, state, identifiers. |
| `parsers.py` | Format-specific parsers for SQL, JSON, logs, handover text, PDFs and images. |
| `store.py` | Loads all evidence and the activity log and builds the lookup indexes. |
| `validate.py` | Checks that every parsed record count reconciles with the raw files. |
| `identity.py` | Exact matching of emails, hashes and phones, current and historical. |
| `policy.py` | Decides present / active / restorable and applies `deletion_rules.csv`. |
| `linker.py` | Ties identity-less accounts and analytics records to cases through the activity log. |
| `pipeline.py` | The audit engine: one request in, one status and ordered steps out. |
| `steps.py` | Orders, numbers and serialises the steps in the required schema. |

### `code/evaluation/` (evaluation code; the reports are in the top-level `evaluation/`)

| File | Role |
|---|---|
| `evaluate.py` | Metrics: status, step and field accuracy, sequence accuracy, per-case differences. |
| `validate_output.py` | Checks `output.csv` against every format rule of the task. |
| `custom_cases.py` | Three additional synthetic cases run through the real engine. |

The `dataset/` folder (the challenge files) and the generated `work/` folder are not part of the zip.

## How it works

### The story in one paragraph

A workflow dashboard says a customer's deletion is "Complete". The agent does not take that on trust. It treats every status and every worker result as a *claim*, rebuilds who the customer is, looks at what the evidence files actually show **at the moment of the audit** (`audit_at`), and applies the company's deletion rules to everything it finds. If all copies are in the required state, it returns the checks that prove it (`COMPLETE`). If not, it returns what remains, the safe order to fix it, and the checks to run afterwards (`INCOMPLETE`). If the person never asked for deletion, it returns nothing to do (`NOT_APPLICABLE`).

### The pipeline at a glance

**Part A. Read the evidence once** (Phase 1)

```
  94 evidence files                      system_activity.csv
  SQL, JSON, logs, text,                 (what workers *claim* happened)
  PDFs, screenshots                                  |
        |                                            |
        v                                            |
  extractors.py   PDFs and screenshots become text   |
        |                                            |
        v                                            |
  parsers.py      one parser per file format         |
        |                                            |
        v                                            v
  normalize.py    every observation becomes one NormRecord:
                  system, resource, category, identifiers, state, time
        |
        v
  store.py        indexes by resource and by file   +  validate.py checks
                                                        that no row was lost
```

**Part B. Audit every request** (Phase 2)

```
  one request: email, reported_status, audit_at
        |
        v
  [1] WHO?             identity.py
        |              exact match on email / hash / phone, current and past
        v
  [2] WHAT DO WE SEE?  pipeline.py
        |              every record tied to that person ........... only evidence
        |              captured on or before audit_at counts
        v
  [3] WHAT IS MISSING? linker.py
        |              accounts and analytics rows carry no identifier: tie them
        |              to the case through the activity log (timing, one-to-one)
        v
  [4] A DELETION REQUEST AT ALL?
        |              no  --->  NOT_APPLICABLE   steps = []
        v  yes
  [5] STATE NOW?       policy.py
        |              latest observation wins; a newer snapshot that covers the
        |              system but does not list the resource proves it is gone
        v
  [6] WHAT DO THE RULES SAY?   deletion_rules.csv
        |              delete / redact / anonymise / retain; disable data flows;
        |              block restores
        v
  [7] BUILD THE STEPS  steps.py
                       dedupe, order by phase, number from 1
                          |
          +---------------+----------------+
          v                                v
     COMPLETE                         INCOMPLETE
     verification steps only,         findings (with evidence)
     each citing evidence             + remediation plan
                                      + verification steps (empty evidence)
```

### The same thing, step by step

| Step | Question the agent asks | Where | What it does |
|---|---|---|---|
| A | What is in the evidence? | `normalize.py`, `parsers.py`, `extractors.py`, `store.py` | Turns the 94 files into one table of records, each with a system, resource id, category, identifiers (emails, user ids, phones, hashes), state, observation time and source file. Activity-log events are kept separately, because they are claims, not proof. |
| 1 | Who is the customer? | `identity.py` | A record belongs to a user only through an exact match on a lower-cased email, hash or phone number (including past values from the audit log), or through a user-id reference. No fuzzy matching, so look-alike emails never match. |
| 2 | What did the evidence show at `audit_at`? | `pipeline.py` | Collects every record tied to the user. Snapshots, exports and documents are judged by their file capture time and log lines by their own timestamp. Evidence from after `audit_at` is ignored. |
| 3 | What can the content not tie to anyone? | `linker.py` | Some accounts and analytics records carry no identifier. They are matched to a case through the activity log using timing, evidence and the worker's reported result, each resource going to at most one case. |
| 4 | Did the person actually ask for deletion? | `pipeline.py` | If the intake classified the request as non-deletion for an account the user owns, the answer is `NOT_APPLICABLE` with an empty step list. |
| 5 | What state was each resource in? | `policy.py`, `pipeline.py` | Present, active or restorable, judged from the latest observation. Scope-aware absence: a newer snapshot that covers the system but does not list the resource shows it is gone. |
| 6 | What do the rules require? | `policy.py` | Applies `deletion_rules.csv` per resource: delete, redact, anonymise or retain, plus disabling data flows and blocking restores. |
| 7 | What is the answer? | `steps.py` | Findings, remediation and verification steps are deduplicated, sorted and numbered. The format is then checked by `evaluation/validate_output.py`. |

## What the data showed and how it shaped the design

I started by investigating the data by hand before writing the engine. In "Phase 0" I traced the 32 sample cases through the 94 evidence files (`code/phase0/`, not part of the submission) to see how each correct answer could be reconstructed from the evidence. Then I wrote the normaliser (Phase 1) and the audit engine (Phase 2), and corrected the engine using the sample scores. The table shows what was found, what I did about it, and how sure I am. "Checked" means it held on all 32 samples. "Inferred" means I saw a pattern but could not confirm it on the real cases.

| What the data showed | What I did about it | How sure I am |
|---|---|---|
| Reported statuses and activity results are claims, and the evidence is in many formats: SQL snapshots with replicas, JSON bundles, logs, text records, PDFs and screenshots. | Turned every file into one table of records with a system, resource, category, identifiers, state and time. Screenshots and PDFs are read with OCR. | Checked |
| An evidence file describes the world at its capture time, not at the time of every section inside it. My first version used section times and leaked future evidence into earlier cases. | Snapshots, exports and documents are judged by the capture time of the file. Log lines are judged by their own timestamps. Anything after `audit_at` is ignored. | Checked (this was the biggest single error in the first run) |
| Policy-retained records (audit records, legal holds) often appear only in files captured later, so a strict time rule hid them. | Retained records are exempt from the visibility cut-off. | Checked on the samples; inferred for the rest |
| Identity must be exact: emails change over time and similar emails belong to different people. | Exact match on lower-cased email, hash and phone, including past values from `user_audit_log.csv`. No fuzzy matching. | Checked, and tested with a look-alike email case |
| A tombstone's `DELETED` account status does not mean the record is absent. | The status field is ignored for tombstones, and the rule from `deletion_rules.csv` (redact, keep the stable reference) is applied. | Checked |
| Many accounts and analytics records carry no identifier, so no file ties them to a user. The deletion events in the activity log sit at a fixed distance before the case's report time (accounts about 45 to 75 minutes, analytics about 28 minutes), and the cleaning events for support messages, data flows and backups follow at fixed offsets. | A linker assigns these resources to cases by timing, with each resource used by one case at most. | Checked on the samples. **This is the weakest part:** where several cases share a report time, an assignment can be wrong (sample cases 17 and 23). |
| The worker's result on an account deletion event matches the case's reported status (`SUCCESS` for `COMPLETE`, `FAILED` for `NOT_STARTED`, `PARTIAL` or `SUCCESS` for `PARTIALLY_COMPLETE`). | Used as a soft penalty when choosing between candidate accounts, not as a hard rule. | Checked on all 31 sample account events; inferred for the rest |
| Some requests are not customer deletions: a classification event in the activity log marks the account, and the security audit ledger ties those accounts to emails. | A case is `NOT_APPLICABLE` only if the classified account belongs to the requesting user. This gives exactly 10 cases. | Inferred; the samples have no such case |
| Ten requests rely on screenshots or PDFs. Link events tie their resources to users, but the documents also contain decoy rows, and old links should not count. | Keep only the linked resources of a document, match links by id or by file alias, and accept a link only if it is at most 72 hours old at `audit_at`. | Hand-checked on the full run |
| About 80 real requests show no audit record in the evidence I could read, while some sample cases expect one. | Nothing is invented. Where no tie exists, the record is left out. | Inferred; it may be a limit of my linking |
| Replica snapshots can contain rows that the primary no longer has (replication drift). | A record counts as present if any copy in the chosen snapshot is present. | Tested on a small case only |

I made the rules as general as the evidence allows, but many of them come from patterns in 32 labelled cases. I expect them to hold on most of the 518 real cases and I cannot prove it. The evaluation report states this and lists the failures that remain.

## Evaluation

`evaluation/evaluation_report.md` has the accuracy numbers, the three additional cases, the failures found and the improvements made. `evaluation/usage_report.md` covers model usage.

## Why no LLM

The system makes every decision with deterministic code and does not call a language model. This was a deliberate choice, and the reasons and the costs are listed below.

**Reasons**
- **The task is rule application over structured evidence.** The rules are in `deletion_rules.csv`. The evidence is SQL tables, JSON, log lines, text and a few scanned images. The answer depends on exact matches of ids, emails and timestamps. Those are things code does exactly and a model can get subtly wrong.
- **Reproducible results.** The same input gives the same `output.csv` on every run. Two runs can be diffed and see exactly what a code change did.
- **Traceable decisions.** For each step it is possible to say which file, which rule and which identifier produced it. The diagnostics file lists them per case.
- **No identity guessing.** Identity depends on exact matches, and a look-alike email must not match. A model should not decide that two similar people are the same person.
- **No sensitive data leaves the machine.** The evidence contains customer emails and phone numbers. Nothing is sent to an outside service.
- **Cost and speed.** A full run costs nothing in tokens and finishes locally.

**Costs accepted**
- **Rules written by hand are brittle.** Where the data does something not seen in the samples, a rule can be wrong. The evaluation report lists the known failures.
- **Some linking is heuristic.** Accounts without any identifier are matched by timing and by the worker's reported result. A model would not have solved that either, because the missing information is not in the text.
- **Text in screenshots depends on OCR quality.** A vision model might read some images better than `tesseract`.
- **An earlier plan** used a model for extracting evidence from free text. It was dropped after the deterministic parsers turned out to cover the evidence, and so that the output stays reproducible.

**Where a model could help later:** summarising the findings of a case in plain language, reading difficult screenshots, and proposing hypotheses about unlinked records for a human to confirm.

## What is not included in the repository

The `dataset/` folder, generated `work/` files, virtual environments, caches and any credentials. No API keys are used or required.
