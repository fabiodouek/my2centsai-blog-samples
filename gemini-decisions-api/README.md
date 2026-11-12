# Getting started with the Gemini Decisions API

Companion code for the my2cents.ai deep dive on the Gemini Decisions API (`gemini-decision-1`):
a first request, a support-ticket triage demo with all three question types, multimodal input,
and measurements of latency, billed tokens, calibration and error handling.

The Decisions API answers closed questions (yes/no, pick one option, rate on a rubric) about one
shared input and returns probabilities instead of text. It bills input tokens only.

## Requirements

- Python 3.11+
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)
- For `05_context_scaling.py` and the 1,000-question run in `04_fanout.py`: a project on the paid
  tier. The free tier allows 50,000 tokens per minute, less than one of those requests.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install .
export GEMINI_API_KEY=...        # never commit it
export DECISIONS_TPM=1000000     # 50000 on the free tier; the scripts pace themselves under it
```

## Scripts

| Script | What it shows |
|---|---|
| `01_first_call.sh` | One yes/no question over curl |
| `01_first_call.py` | The same through google-genai 3.2, and why the client must be pinned to `v1beta` |
| `02_ticket_demo.py` | One support ticket, yes/no + choice + score questions in one request, and a triage rule on the answers |
| `03_multimodal.py` | The ticket with a screenshot, a voice note, a screen recording and a PDF; tokens billed per file and resolution; the Files API |
| `04_fanout.py` | 1, 10, 100 and 1,000 questions over one input: latency and billed tokens |
| `05_context_scaling.py` | Latency from about 1K to about 250K input tokens |
| `06_calibration.py` | 500 labeled news articles: accuracy, reliability table, ECE, run-to-run determinism |
| `07_edge_cases.py` | No option fits, option order, prompt injection, and the real error bodies (`TRY_429=1` adds a rate-limit burst) |
| `08_flash_lite_baseline.py` | The same 500 articles through Gemini 3.5 Flash-Lite with an enum output: accuracy, latency, cost |
| `09_cleanup.py` | Deletes Files API uploads and prints what the scripts spent |

Each script prints its results and writes them to `results/`. The JSON files committed there are
from my runs.

## Sample data

- `assets/` holds the synthetic ticket media: a drawn app screenshot, a generated PDF statement, a
  computer-voiced voice note (17 s) and a 12-second screen recording. `make_assets.py` rebuilds
  them (`pip install '.[assets]'`, needs ffmpeg and macOS `say`).
- `06` and `08` download the AG News test set on first use and take a fixed, seeded sample of 500.

## Cost

Gemini Decision 1 costs $0.10 per 1M input tokens and bills no output. `results/spend.json` keeps a
running total of what the scripts sent.

## Cleanup

```bash
python 09_cleanup.py     # Files API uploads
```

Then delete the API key in [AI Studio](https://aistudio.google.com/apikey), or with
`gcloud services api-keys delete KEY_ID`.
