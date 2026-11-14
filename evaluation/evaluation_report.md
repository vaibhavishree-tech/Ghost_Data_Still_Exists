# Evaluation report: Ghost Data deletion-audit agent

## 1. Summary

| Item | Result |
|---|---|
| Sample cases scored | 32 (`dataset/sample_deletion_requests.csv`, compared with `dataset/sample_output.csv`) |
| Case-level status accuracy | **1.000** (32 of 32) |
| Step precision / recall / F1 | 0.984 / 0.969 / **0.977** |
| Step F1 ignoring `evidence_id` | 0.982 |
| Sequence exact match | 0.781 (25 of 32 cases have an identical step list) |
| Sequence LCS ratio | 0.952 |
| Additional cases (written by me) | 3 of 3 pass (section 5) |
| Full run | 518 rows, format check passes: 352 INCOMPLETE, 156 COMPLETE, 10 NOT_APPLICABLE |
| Model usage | none; the system is deterministic (see `usage_report.md`) |

**Read this before trusting the numbers.** The 32 sample cases are the only cases with known answers. I used them to find and fix errors, so the sample scores are optimistic. I have no labels for the 518 real requests, so their accuracy has not been measured. Section 6 lists the known weak spots.

## 2. What was measured and how

The scorer is `code/evaluation/evaluate.py`. It compares the output with `sample_output.csv` and runs through `python code/main.py --sample`.

- **Status accuracy:** the share of cases whose `deletion_status` equals the expected one.
- **Step precision, recall, F1:** each step is a tuple of (phase, system_id, resource_id, operation, state, evidence_id). Precision is the share of the produced steps that are expected. Recall is the share of expected steps that the output contains. I also report the same scores with `evidence_id` ignored, to separate "found the right record" from "cited the right file".
- **Field-level accuracy:** the expected and predicted step lists are aligned by position, and each field is compared separately. A single missing step shifts the later positions, so this view is stricter than the set-based F1.
- **Sequence accuracy:** exact match counts a case only if the whole ordered step list is identical. The LCS ratio is the longest common subsequence of the two ordered lists, normalised (see `evaluate.py` for the exact normalisation); it gives partial credit for near-misses.
- **Format validation:** `code/evaluation/validate_output.py` runs after every full run. It checks the row count, ids and order, valid JSON, consecutive `step_no`, phase and state consistency, operation order, duplicate steps, `[]` only for NOT_APPLICABLE, COMPLETE cases containing verification only with evidence, and INCOMPLETE cases having findings that match their remediation.

## 3. Results on the 32 sample cases

### 3.1 Status accuracy

32 of 32 correct (1.000). In the sample, 10 cases are COMPLETE and 22 are INCOMPLETE; the sample has no NOT_APPLICABLE case.

### 3.2 Step level

| Metric | With `evidence_id` | Ignoring `evidence_id` |
|---|---|---|
| Precision | 0.984 | 0.989 |
| Recall | 0.969 | 0.974 |
| F1 | 0.977 | 0.982 |

The expected answers contain 193 steps. Ours match 187 of them exactly.

### 3.3 Field level (position aligned)

| Field | Accuracy |
|---|---|
| step_no | 0.979 |
| phase | 0.979 |
| system_id | 0.943 |
| resource_id | 0.938 |
| operation | 0.943 |
| state | 0.943 |
| evidence_id | 0.969 |

### 3.4 Sequence

Exact match is 0.781 (25 of 32 cases). The LCS ratio is 0.952.

### 3.5 Remaining sample differences

Seven cases still differ from the expected answer. The cause notes are from reading the per-case diagnostics; the ones marked "not confirmed" are my best explanation, not a verified one.

| Case | Difference | Likely cause |
|---|---|---|
| case_03 | missing `VERIFY_RECORD_ANONYMIZED` for `analytics_9406` | The analytics record is linked to the case through the activity log but produced no step (cause not confirmed). |
| case_04 | missing `VERIFY_RECORD_ABSENT` for `account_004` | The sample uses hand-made ids (`account_00X`); the account was not linked to this case (not confirmed). |
| case_09 | `REPORT_RESTORE_RISK` for `backup_4978` cites `evidence_06`, expected `evidence_93` | The right record and the right step, but the output cites a different file that also shows it. |
| case_17 | extra `VERIFY_RECORD_ABSENT` for `account_6523` | Wrong account chosen from several time-compatible candidates. |
| case_18 | missing `VERIFY_RECORD_PRESENT` for `audit_record_6645` | The retained audit record has no identifier that ties it to the user in any file I could read. |
| case_20 | missing `VERIFY_RECORD_PRESENT` for `audit_record_5569` | Same as case_18. |
| case_23 | expected `account_410`, the output has `account_7015` | Two deletion events fit the time window and both report `FAILED`; nothing in the data separates them. |

## 4. Full run (518 requests)

| Item | Value |
|---|---|
| Rows written | 518 (plus header) |
| INCOMPLETE | 352 |
| COMPLETE | 156 |
| NOT_APPLICABLE | 10 (cases 533 to 542) |
| Accounts linked through the activity log | 303 |
| Analytics records linked through the activity log | 123 |
| Format validation | passes |

There are no labels for these requests, so the table reports counts, not accuracy.

Two properties I checked by hand on the full run:
- **Screenshot and PDF cases.** Ten requests (33, 35, 39, 45, 50, 52, 53, 55, 61, 63) rely on OCR of screenshots and PDFs. After the fixes in section 7 they all return steps, and no row is `COMPLETE` with an empty list.
- **NOT_APPLICABLE.** A request is marked NOT_APPLICABLE only when its account was classified as "not a customer deletion" in the activity log and the account is owned by the requesting user. Exactly 10 cases qualify.

## 5. Additional evaluation cases

The cases are in `code/evaluation/custom_cases.py`. Each builds a small dataset in the same file formats as the real one and runs it through the real engine (`python code/evaluation/custom_cases.py`). The expected answers follow my reading of `deletion_rules.csv` and the sample answers; they are not official answers.

| Case | Scenario | What it checks | Expected result | Outcome |
|---|---|---|---|---|
| case_901 | Look-alike email: the requester `jordan.lee@gmail.com`, and a different person `jordan.lee1@gmail.com` with an account and a search entry | Identity is an exact match. The other person's records must never appear. | INCOMPLETE; the requester's account only (finding, delete, verify) | PASS |
| case_902 | Replica drift: a search entry is absent from the primary table but still present in the read replica | Every copy in a snapshot is compared; a clean primary alone is not enough. | INCOMPLETE; remaining record found on the replica | PASS |
| case_903 | Re-enabled data flow: the connector is disabled on day 1, enabled on day 3 (before the audit time), and disabled again on day 6 (after the audit time) | The latest state at `audit_at` decides; evidence from after `audit_at` is ignored. | INCOMPLETE; active data flow found, disabled, verified | PASS |

Aggregate on these cases: status accuracy 1.000, step F1 1.000, sequence exact match 1.000.

**Limits of these cases.** The cases are small and clean, I wrote them after the engine was tuned, and they cover three behaviours, not the whole rule set. In case_903 every log line carries a `correlation_key`, so the test targets time ordering and not how a flow gets tied to a user. They are spot checks, not an accuracy estimate.

## 6. Known limitations

1. **Tuned on 32 samples.** Several rules (time windows for linking deletion events, the cleaning-event offsets, the 72-hour validity of a resource link) were derived from the sample answers. They may not hold on every real case.
2. **Account linking is the weakest part.** Many real requests have no direct identifier for their account. The engine assigns accounts from the activity log by time and by the worker's result, one-to-one across all cases. Where several cases share a report time, an assignment can be wrong (see case_23 and case_17).
3. **Some retained records cannot be recovered.** About 80 real requests have no audit record, which may be by design. Where an audit record has no identifier tying it to the user, it cannot be included (case_18, case_20).
4. **Replica drift is minimal.** A record counts as exposed if any copy in the chosen snapshot is exposed. Finer replication-lag reasoning is not implemented.
5. **OCR dependence.** Screenshot and PDF evidence goes through `tesseract` and `pdftotext`. Misreads there could change a result; the ten screenshot cases were checked by hand.
6. **Row order.** `output.csv` is written in numeric case order by default (`--order lexicographic` is available). The case ids are unpadded, so numeric order is also the file order.

## 7. Failures found and improvements made

I iterated on the 32 samples first and then on the full run. The rounds group several small patches, so the F1 shown next to each round is the value measured after it and is only approximately attributable to that round.

| Round | Failure found | Improvement | Result |
|---|---|---|---|
| 1 | First run: status accuracy 0.94, step F1 0.62. Evidence from after the audit time leaked in because file sections were judged by their own time instead of the file capture time. A tombstone state `DELETED` was read as "absent". Categories were assigned by id prefix only. `eligible` / `deletion_block` fields were not parsed. The log filter was too strict. | Judge snapshots, exports and documents by file capture time and log lines by their own time. Read tombstone states correctly. Derive the category from the record's event and path, then fall back to the declared category and prefix. Parse restore flags. Relax the log filter. | Large rise in F1 |
| 2 | NOT_APPLICABLE false positives in cases 13, 21, 23, 27, 28 and 29 (status accuracy fell to 0.81). My time-window rule collided with cases that share a report time. | Replace the time window with ownership: a case is NOT_APPLICABLE only if its classified account is owned by the requesting user, using all evidence. | Status accuracy 1.000, F1 0.942 |
| 3 | Audit records missing in INCOMPLETE cases: they appear only in later files, so the visibility rule dropped them. Log lines carrying an email were dropped as uninformative. 25 cases lacked an account step because strict evidence matching rejected the right candidates. | Retained record types (audit record, legal hold) ignore the visibility cut-off. Log lines with an identifier count as informative. Time-only account candidates are kept at a penalty. | F1 0.951 |
| 4 | The full run produced `case_61,COMPLETE,[]`, which is invalid. The deletion log described the screenshot by file name instead of by id. Decoy rows in the screenshots of cases 33, 35, 45 and 52 were included. Two sample cases picked up old link events. | Match link events to documents by id or by alias. Keep only the linked resources of a document. Accept a link only if it is at most 72 hours old at the audit time. Add an `EMPTY_COMPLETE` check. | F1 0.961; no invalid rows in the full run |
| 5 | Account swaps in cases 22, 31 and 32, which share report times with other cases. | Compare the deletion event's reported result with the case's reported status (`COMPLETE` with `SUCCESS`, `NOT_STARTED` with `FAILED`, `PARTIALLY_COMPLETE` with `PARTIAL` or `SUCCESS`) and add a penalty on a mismatch. This held for all 31 account events in the samples (two `PARTIALLY_COMPLETE` cases reported `SUCCESS`, so that status accepts both). | F1 0.977; sequence exact match 0.688 to 0.781 |

A rule that worked in the samples was used only as a soft penalty (round 5), because a hard filter would break on any real case that is an exception.

I also added automatic format validation after every full run, after the `[]` case showed that a plausible-looking row can be invalid.

## 8. How to reproduce

```
python code/main.py --sample                      # scores the 32 sample cases, writes work/phase2/sample_eval.md
python code/main.py                               # writes output.csv at the repository root, then validates it
python code/evaluation/custom_cases.py            # runs the three additional cases
```

Requirements and setup are described in `README.md`. Any model usage is in `usage_report.md`.