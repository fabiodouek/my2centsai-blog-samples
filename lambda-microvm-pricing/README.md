# AWS Lambda MicroVMs Pricing Calculator

A small calculator that rebuilds the two [AWS Lambda MicroVMs pricing examples](https://aws.amazon.com/lambda/pricing/) line by line, explains them step by step from a single MicroVM, and takes your own numbers. Plus `bill.py`, which prints your actual MicroVM usage from Cost Explorer so you can compare.

Companion code for [AWS Lambda MicroVMs Pricing, Explained](https://my2cents.ai/deep-dive/lambda-microvms-pricing).

Video walkthrough: https://youtu.be/s3k7A1qLJx0

## Requirements

- Python 3.11+. `microvm_cost.py` uses the standard library only.
- For `bill.py`: boto3 and AWS credentials allowed `ce:GetDimensionValues` and `ce:GetCostAndUsage`. Each run makes two Cost Explorer API calls, which AWS bills at $0.01 each.

```bash
uv venv && uv pip install -e '.[bill]'   # or: pip install boto3
```

## Reproduce AWS's examples

```bash
python microvm_cost.py scenarios/coding-sandboxes.json   # Pricing Example 1: $1,241.22, $12.41 per developer
python microvm_cost.py scenarios/ci-jobs.json            # Pricing Example 2: $1,124.03, $0.1124 per job
python -m unittest                                       # both match the pricing page to the cent
```

```text
Pricing Example 1: sandboxed environments for coding assistants
...
Compute
  Peak                  4.50M s at 4 vCPU / 8 GB                           $630.50
  Baseline              13.50M s at 1 vCPU / 2 GB                          $472.88
  Compute total                                                          $1,103.38
Snapshot read/write
  Write on suspend      12,000 cycles x 2 GB                                $91.20
  Read on resume        12,000 cycles x 2 GB                                $37.20
  Read on launch        2,000 launches x 2 GB                                $6.20
  Read/write total                                                         $134.60
Snapshot storage
  Images                5 x 2 GB for a month                                 $0.80
  Suspended MicroVMs    2,000 x 2 GB x 5.5 h / 720 h                         $2.44
  Storage total                                                              $3.24
----------------------------------------------------------------------------------
Monthly total                                                            $1,241.22
Per developer                                                               $12.41
```

## Build it up from one MicroVM

`--steps` explains a scenario the way the post does: one MicroVM for an hour, the burst, one suspend/resume cycle and its break-even, one session, then the month.

```bash
python microvm_cost.py scenarios/coding-sandboxes.json --steps
```

```text
Step 1: one MicroVM at its 1 vCPU / 2 GB baseline
  per second  1 x $0.0000276944 + 2 x $0.0000036667 = $0.0000350278
  per hour    $0.1261   (vCPU $0.0997, memory $0.0264)
...
Step 3: suspend and resume (2 GB of state)
  one cycle   write $0.0076 + read $0.0031 = $0.0107
  break-even  suspending pays off after about 5.1 idle minutes
Step 4: one session
  ...
  session total                     $0.6202
  without suspending (idle at baseline instead): $1.25
```

## Your own numbers

Copy a scenario file, or override fields with `--set`:

```bash
python microvm_cost.py scenarios/coding-sandboxes.json --set state_gb=1.1 --set cycles=10
python microvm_cost.py scenarios/ci-jobs.json --set sessions=2500 --set peak_share=0.3
python microvm_cost.py scenarios/coding-sandboxes.json --rates price-list   # the unrounded rates your bill uses
python microvm_cost.py scenarios/ci-jobs.json --json                        # line items as JSON
```

| Field | Meaning |
|---|---|
| `baseline_gb` | Baseline memory. vCPU is memory / 2. |
| `peak_gb` | Size while bursting, at most 4x the baseline. |
| `sessions` | MicroVM sessions a month (a developer-day, a job). Each launches once. |
| `active_seconds` | Running time per session. |
| `peak_share` | Share of the running time at `peak_gb` (0 to 1). |
| `suspended_seconds` | Suspended time per session. Running plus suspended can't exceed 8 hours. |
| `cycles` | Suspend/resume cycles per session. |
| `state_gb` | GB written on each suspend and read on each resume. |
| `launches_per_session`, `image_gb` | Launch reads: GB read from the image per launch. |
| `images` | Images kept for the month: `[{"count": 5, "gb": 2}]`. |
| `per` | How to divide the total (`{"label": "developer", "divisor": 100}`). |

The calculator warns when a scenario can't exist: a peak above 4x, or a session longer than the 8-hour lifetime.

What it doesn't model: data transfer, the idle window before auto-suspend (billed as running; add it to `active_seconds`), the one-week image minimum, and any egress infrastructure in your VPC. Snapshot sizes are whatever you put in `state_gb` and `image_gb`; the post explains why the billed size isn't the same as the memory snapshot.

## Check your bill

```bash
python bill.py                                  # the last 30 days
python bill.py --start 2026-11-01 --end 2026-11-08
```

```text
usage type                                        quantity       cost            rate
Lambda-MicroVM-vCPU-Second-ARM                  43,346.919     1.2005    0.0000276944  per vCPU-s
Lambda-MicroVM-Snapshot-Read-GB                    677.318     1.0477    0.0015467699  per GB
Lambda-MicroVM-Memory-GB-Second-ARM             85,410.589     0.3132    0.0000036667  per GB-s
Lambda-MicroVM-Snapshot-Write-GB                    61.593     0.2339    0.0037977138  per GB
Lambda-MicroVM-Snapshot-Storage-GB-Hour          1,952.325     0.2169    0.0001111111  per GB-h
total                                                          3.0121
```

That's the bill for the three-part MicroVMs series on my2cents.ai. Cost Explorer lags by up to a day and reports days, not hours (hourly granularity is opt-in on the payer account), so it can't split usage per MicroVM.

## Rates

The defaults are us-east-1, Graviton (ARM), from the pricing page as of November 9, 2026. `page` uses the five-decimal snapshot rates the examples use; `price-list` uses the unrounded rates the bill charges. Other Regions price differently (about +5% in eu-west-1, +27% in eu-central-1): edit `RATES` in `microvm_cost.py`.
