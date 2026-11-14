# Usage report

This report covers the final full-dataset run that produced `output.csv` (`python code/main.py`, 518 requests).

**The system does not use any language model.** Every decision is made by deterministic code and no external service is called during a run. The figures below are therefore zero. They are not estimates.

## Overall

| Item | Value |
|---|---|
| Model providers | none |
| Model names | none |
| Model calls | 0 |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens per request (518 requests) | 0 |
| Estimated total cost | $0.00 |
| Estimated cost per request | $0.00 |

## Per model

No models were used, so there are no per-model rows.

## What does run

- Evidence parsing (SQL, JSON, logs, text, PDF) is plain Python code.
- Screenshot text is read with the local `tesseract` program, and PDF text with `pdftotext`. These are local tools, not model API calls, and they have no per-use cost.
- No API keys or credentials are needed or included.

See the "Why no LLM" section of `README.md` for the reasons behind this choice.