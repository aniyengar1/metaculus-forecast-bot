# Stage B — decisions and math

## OpenRouter free-tier rate limits vs our call volume

Source: OpenRouter's own FAQ (confirmed via direct fetch 2026-10-08) plus several
third-party trackers, all agreeing: for any model whose ID ends in `:free`, the
limit is **account-wide across all free models combined** (not per-model):

- **20 requests/minute**, always.
- **50 requests/day** if the account has never purchased OpenRouter credit.
- **1,000 requests/day** once the account has purchased **$10 of credit at any
  point in its history** (one-time, sticks even if balance later hits zero).
  This is a real-money OpenRouter account purchase, **unrelated** to Metaculus's
  sponsored *model* credits (those just mean OpenRouter calls through the
  tournament's shared key don't cost the tournament/us anything -- they don't
  touch this separate per-account rate-limit tier).

### Our per-question call count

**Update 2026-10-08**: originally this used the LLM-based `structure_output`
parser for every model run (`num_validation_samples=2`), costing 10
requests/question nominal. That's been replaced with deterministic regex
parsing (`metaculus_bot/deterministic_parse.py`, matching our prompts' exact
specified output formats) tried first, falling back to a single LLM parser
call (`num_validation_samples=1`, down from 2) only when regex can't make
sense of a response, with the fallback logged and recorded per-model
(`parse_method` in the DB).

Measured against the same 5 live questions used throughout Stage A/B testing:
**13 of 15 model-runs (87%) parsed deterministically**, 2 fell back. Real
average: **22 requests / 5 questions = 4.4 requests/question**.

| Call | Count |
|---|---|
| Research summarization (1x, to `parser_model`) | 1 |
| Ensemble forecast calls (1 per model, `n_runs`=3) | 3 |
| LLM parser fallback (only on deterministic-parse failure, ~13% of model-runs observed) | ~0.4 avg |
| **Total (nominal / observed average)** | **4 / ~4.4** |

Plus a **model health check** (`metaculus_bot/model_health.py`): 1 request per
candidate in `model_pool` (6 by default) = **~6-10 requests**, including any
429 backoff retries during the check itself -- but only when a run actually has
new questions to forecast (main.py fetches + filters + throttles *before*
health-checking, specifically to avoid burning this on empty runs).

Since the limit is account-wide, *which* model each call goes to doesn't matter --
only the total. With a ~20% safety margin over the observed 4.4 average, call it
**~5 requests/question**, plus **~10** for one health-check pass.

### Daily budget

- **No-credit tier (50/day)**: reserve ~10 for one health-check pass → 40 left →
  **40 / 5 ≈ 8 questions/day** safely. This is what `MAX_QUESTIONS_PER_DAY`
  defaults to (and the `MAX_QUESTIONS_PER_DAY` repo Variable is set to).
- **Paid tier (1000/day, after a one-time $10 OpenRouter purchase)**: reserve
  ~20 for several health-check passes → 980 left → **980 / 5 ≈ 195
  questions/day** (not needed yet -- no OpenRouter purchase planned while
  waiting to see if Metaculus's sponsored credits arrive).

### Why this matters for MiniBench specifically

MiniBench rounds run ~60 questions over ~2 weeks (~4.3 new questions/day on
average per Phase 1 research, likely bursty rather than smooth -- e.g. many
questions could open at round start). At **8 questions/day** (no-credit tier,
now that deterministic parsing cut the per-question cost), a full 60-question
burst clears in **~7.5 days** -- comfortably inside the ~14-day round. Throttled
questions aren't lost (they're just deferred to a later run, since
`skip_previously_forecasted_questions` means we retry anything not yet
forecast). The free tier now looks adequate for MiniBench at realistic volume,
so no OpenRouter purchase is needed for this specifically -- matches the
decision to hold off and see whether Metaculus's sponsored credits arrive
first. `MAX_QUESTIONS_PER_DAY` is still a repo Variable
(`vars.MAX_QUESTIONS_PER_DAY`) in case actual arrival rates turn out burstier
than this estimate and it needs tuning without a code change.

The cron interval (every 2 hours, not the template's every 20 min) is chosen
independently of this -- the 20-min cadence would've meant up to 12x more
health-check passes/day for no benefit once the daily question cap is already
the binding constraint.

## Why a `data` branch, not a GitHub Actions artifact, for `data/forecasts.db`

Both were considered for persisting the SQLite log across workflow runs
(required for the daily cost/question throttles to actually work across
separate CI containers, not just local runs):

- **Artifact**: scoped to a single workflow run; "get the latest one from any
  previous run" needs either the GitHub API or a third-party action
  (`dawidd6/action-download-artifact`), has a retention window (default 90
  days), and isn't browsable -- you'd have to download it to inspect it.
- **Data branch** (what we built, `scripts/{restore,persist}_data_branch.sh`):
  plain git, no extra actions needed. A `git worktree` checks out (or creates,
  as an orphan) a `data` branch holding only `data/forecasts.db`, so this never
  touches the main branch's working tree. Since this repo is public anyway, a
  normal `git clone` being enough to inspect the full forecast history is a
  feature. Tradeoff: a binary blob slowly growing the repo's history -- at a
  few KB per run this is a non-issue at our scale, and if it ever matters,
  `git filter-repo` can prune old blobs from that one branch without touching
  `main`.

Both the MiniBench and Test Bot workflows restore before running and persist
(`if: always()`, so a mid-run failure doesn't lose the DB state) after, sharing
the same `data` branch -- intentional, since both draw on the same real
OpenRouter account and should see each other's spend/question counts when
computing the daily throttles.

## Follow-ups 2026-10-09

**Soonest-close-time prioritization.** When a `--limit` or the daily throttle
means not every open question gets forecast this run, we now sort by
`close_time` ascending first (`main.py`), so a tight cap drops the questions
with the most runway left, not an arbitrary subset. Every open question we
see gets upserted into a new `seen_questions` table (question id, close_time,
a `forecasted` flag) regardless of whether we get to it this run; each run
also checks that table for anything that closed while still unforecast and
logs it to `alerts` (`db.find_newly_closed_unforecast`) -- a question being
silently missed is now something we'd actually notice.

**OpenRouter quota pre-flight guard.** `GET /api/v1/key` (metadata, doesn't
spend a request) reports `free_model_daily_requests: {used, limit,
remaining}` for the account. `main.py` checks this before any local or
manually-dispatched run and refuses to start (`ENFORCE_RATE_LIMIT_GUARD=true`,
the default) if remaining quota is below what the next scheduled production
run needs (`MIN_QUOTA_RESERVE_FOR_SCHEDULED_RUN=15` = ~1 health-check pass +
~1 question). The actual cron-triggered run is exempt
(`GITHUB_EVENT_NAME == "schedule"`, set automatically by GitHub Actions only
for schedule triggers, never `workflow_dispatch` or local) -- the guard exists
to protect that run, so it can never be the thing blocking itself.

Built after confirming the problem was real, not hypothetical: a `GET
/api/v1/key` check mid-writing-this showed `used: 53, limit: 50, remaining: 0`
-- our own Stage A/B testing earlier had already exhausted the account's
real daily cap.
