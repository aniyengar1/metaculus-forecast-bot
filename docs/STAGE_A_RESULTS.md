# Stage A results — dry run, 2026-10-05

Dry run (`--mode tournament --target both --limit 5 --type-diverse`, no `--publish`)
against live open questions in `fall-futureeval-2026` + current MiniBench. Nothing
was submitted to Metaculus. Full log: available on request (not committed; contains
full LLM reasoning text, which is verbose but not sensitive).

## Scope note

`fall-futureeval-2026` (tournament ID 33121) currently has **0 open questions** —
all 5 test questions came from MiniBench (7 open at the time of the run). This is
expected (season just started 2026-09-28) and not a bug; worth re-running this same
dry-run command once the seasonal tournament has live questions, before Stage B.

## Per-question results

| # | Type | Question | Final prediction | Cost | Models OK / failed |
|---|------|----------|-------------------|------|---------------------|
| 1 | Binary | [La Jolla tide >6.0ft Oct 15-16](https://www.metaculus.com/questions/45909) | 46.86% | $0.00 | 2/3 (gemma 429) |
| 2 | Multiple Choice | [WastewaterSCAN norovirus level Oct 15](https://www.metaculus.com/questions/45904) | Low 36.8% / Medium 52.6% / High 10.5% | $0.00 | 3/3 |
| 3 | Discrete (numeric) | [Crypto Fear & Greed Index Oct 16](https://www.metaculus.com/questions/45907) | 5-point CDF, see below | $0.00 | 3/3 |
| 4 | Binary | [US naval blockade on Iran crude still in effect Oct 15](https://www.metaculus.com/questions/45908) | 87.72% | $0.00 | 2/3 (gemma 429) |
| 5 | Binary | [Valencia school suspension Oct 15-16](https://www.metaculus.com/questions/45906) | 11.02% | $0.00 | 2/3 (gemma 429) |

Question #3's representative percentiles: P10≈40, P20≈53, P42≈66, P65≈78, P89≈91 —
a full 201-point PCHIP-smoothed CDF was built and passed the SDK's own validity
checks (bound pinning, monotonicity, max-per-bin PMF cap) before aggregation.

**Total cost: $0.00** (all 5 questions, all models free-tier). Budget guard never
triggered a skip (estimate was $0 throughout, as expected with `:free` models).

## What failed, and why

`openrouter/google/gemma-4-26b-a4b-it:free` hit HTTP 429 (upstream rate limit on
OpenRouter's shared free pool, not our account specifically) on 3 of 5 questions,
despite the 1.5s/run stagger added mid-session to reduce collision odds. The other
two free models (nvidia nemotron, qwen) never failed. This is a known risk of
relying on free-tier shared pools: congestion is server-side and outside our
control. The ensemble degraded gracefully in every case — log-odds median over the
2 surviving models, logged and reasoned about normally, no question was lost.

No structured-output parse failures occurred (binary/MC/numeric percentile parsing
all succeeded on every model that returned text).

## What this validates

- End-to-end pipeline works on **real** live questions across all 3 target types
  we have today (binary, multiple choice, discrete/numeric). Date questions and
  AskNews weren't exercised (none open; AskNews creds still pending), so those two
  paths are implemented but only unit-tested, not live-tested yet.
- Research fallback (DuckDuckGo, free, no API key) worked on every question and
  produced usable context — AskNews absence didn't block anything.
- Real-dollar cost tracking via `forecasting_tools`' `MonetaryCostManager` (backed
  by litellm/OpenRouter's actual reported cost) confirms free-tier calls really
  cost $0 — validates the budget guard's `:free` fast-path.
- PCHIP numeric aggregation path exercised end-to-end on a real discrete question,
  not just the earlier synthetic unit test.
- SQLite logging captured every field specified: research summary, all raw
  per-model outputs (including the 3 gemma failures with their error text),
  aggregate, post-calibration value (identity here, k=1.0), and what-would-be-submitted
  value, per question.

## Update 2026-10-07 — qwen retired, hand-picked swap wasn't enough

Swapped gemma for `apodex/apodex-1.1-mini:free` and added retry-with-backoff (max 3
tries) on 429s, then re-ran the same 5 questions. apodex worked perfectly (5/5), but
`qwen/qwen3.8-27b:free` -- which had been 100% reliable two days earlier -- now
failed on *every* question with `404 NotFoundError`: "This model is unavailable for
free. The paid version is available now." Confirmed via OpenRouter's live model
list: there are no free qwen models at all anymore. Two different models broke in
two different ways (transient 429, permanent retirement) within 48 hours.

## Update 2026-10-08 — self-healing roster

Hand-picking replacements doesn't scale against this churn rate, so the ensemble is
no longer a fixed list: `metaculus_bot/model_health.py` health-checks a 6-candidate
pool (`openrouter/{nvidia/nemotron-3-super-120b-a12b,apodex/apodex-1.1-mini,
qwen/qwen3.8-27b,google/gemma-4-26b-a4b-it,inclusionai/ling-3.0-flash-sante,
dots-studio/dots-3-note-preview}:free`, 6 distinct providers) at the start of every
run, retrying 429s with backoff (`llm_retry.invoke_with_backoff`, shared with the
main forecast calls) and treating anything else (404, auth, etc) as permanently
dead for that run. The first 3 healthy candidates are selected, preferring one per
provider; the model used for parsing/summarizing is also drawn from this healthy
set (previously hardcoded, which was itself a single point of failure). Fewer than
2 healthy models aborts the run entirely and writes to a new `alerts` table rather
than submitting a 1-model "ensemble."

Re-ran the same 5 questions again. Health check: qwen still dead (404, confirmed
real), gemma still rate-limited (429 x2 retries exhausted during the health check
itself), nemotron/apodex/inclusionai/dots-studio all healthy. Selected nemotron +
apodex + inclusionai (3 different providers; dots-studio was healthy but unneeded
once 3 were found). Result: **3/3 models answered on all 5 questions**, $0.00 total
cost, no alerts triggered. `models_used` in SQLite now reflects the actual
post-health-check roster rather than a static config value (confirmed column
already existed; previously it recorded the static default list rather than what
was health-checked, since there was no health check yet).

One scope note on `models_used`: health-checking happens once per process
invocation, not per question, so the column is identical across every row from the
same run. A model that dies mid-run *after* passing the health check is still
caught (per-model error capture in `raw_model_outputs` already handles that), it
just wouldn't change `models_used` for that run.
