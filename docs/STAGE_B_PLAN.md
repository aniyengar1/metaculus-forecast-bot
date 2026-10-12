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

## MiniBench "0 open questions" diagnosis, 2026-10-11

Investigated whether `forecasting_tools.get_all_open_questions_from_tournament`
was dropping real open questions via a filter bug.

**1. Unfiltered API hit.** `GET /api/posts/?tournaments=minibench&limit=100`
(no status param) returned **59 questions total, all `status: closed`**, and
every single one had `open_time`/`scheduled_close_time` exactly **3.0 hours**
apart (min=max=3.0h across all 59 -- not a round-long window per question).
`open_time` range: 2026-10-05 00:07 UTC to 2026-10-07 20:38 UTC. Nothing newer
exists in the API (`offset=100` page is empty). So this round's full ~59-60
question batch already opened *and closed* entirely within its first ~3 days,
each question living only 3 hours -- not spread continuously across the
Oct 5-23 window as the site's date range might suggest.

**2. Filter comparison.** `forecasting_tools`' filter is just
`allowed_statuses=["open"]`. Since our own unfiltered pull agrees 100% of
real rows are `closed`, filtering to `open` correctly returns 0 -- **not a
filter bug.** `already_forecasted`-exclusion, our close-time sort, and the
daily cap are all downstream of this fetch and never even execute when 0
questions come back, so none of those contributed either.

**3. Why 0 right now, and is our schedule fast enough.** We weren't running
yet when this batch's window was open (first real commit/deploy was
2026-10-08-09, this batch had already fully closed by 2026-10-07 23:38) --
we simply weren't there for it, not a bug. We can't predict exactly when the
*next* batch appears (the Oct5-7 window looks like a steady ~1 new
question/1.15h drip rather than a single release, so either the round pauses
between batches or something else determines cadence -- not enough signal to
say which). What we *can* confirm mechanically: every observed question had a
3-hour open window, and our schedule polls every 2 hours (< 3h), so once a
next batch starts, we should catch each question within its window with
margin, same as this diagnosis itself (`main.py --target minibench`) finding
`total=59 (closed=59) -- of 0 open: ...` -- i.e. confirmed working, nothing to
fix here, just waiting on the tournament.

**4. Per-run breakdown logging.** Added to `main.py`
(`_fetch_one_tournament_with_diagnostics`): every run now fetches the
tournament *unfiltered* (not just the open-only call) and logs
`total=N (status=count, ...) -- of M open: K already forecasted, J still need
forecasting` per tournament, so a future "0 found" is immediately
distinguishable as "genuinely nothing open" vs "something's wrong" without
needing to re-run this manual investigation. Also added a run-level alert
(`attempted_count > 0 and successful_count == 0`): if we had open questions,
quota, and a healthy enough model pool to actually attempt them, but every
individual attempt still failed, that's now a logged `alerts` row + loud
print, not silence.

## Multi-provider pool, 2026-10-12

Added Groq and Gemini (both genuinely free, no card on file) as providers
alongside the OpenRouter :free fallback tier. `DEFAULT_MODEL_POOL` is now
hand-ordered by priority: `gemini/gemini-3.8-flash`, `groq/openai/gpt-oss-120b`,
`groq/qwen/qwen3.8-27b`, then the OpenRouter pool. `model_health.py` dropped
its provider-diversity-dedup logic in favor of pure priority order, since the
pool itself now intentionally repeats "groq" twice -- the old dedup would
have skipped the second Groq model and jumped straight to an OpenRouter
fallback instead, fighting the intended ordering. Verified via `GET
/v1beta/models` + individual invokes (2026-10-12) that every current Gemini
Flash/Flash-Lite variant (3.1 through 3.8) responds on the free tier, and
via `GET /openai/v1/models` that Groq actually hosts `openai/gpt-oss-120b`,
`openai/gpt-oss-20b`, and `qwen/qwen3.8-27b` right now.

**Per-provider quota** (`metaculus_bot/provider_quota.py`): OpenRouter stays
live-read (`/api/v1/key`, account-wide). Groq has published fixed per-model
limits (1000 RPD each, independent per model -- not pooled like OpenRouter)
that we track our own usage against. Gemini publishes nothing and returns no
quota headers on success (checked real response headers: only
`x-gemini-service-tier`, no quota-remaining); we track our own usage against
a guessed 1000 RPD ceiling until a real 429's `QuotaFailure` body teaches us
the actual number, then remember it permanently. `compute_daily_question_budget`
takes the tightest per-model constraint (remaining quota ÷ expected
calls/question for that model's role -- 1.0 for an ensemble slot, ~1.4 for
the parser role, measured 2026-10-08/09), applies a 20% margin, and caps at
`MAX_QUESTIONS_PER_DAY_CEILING` (now a safety ceiling, not the primary
driver). The parser/summarizer role is a separate pool
(`gemini/gemini-3.5-flash-lite`, `groq/openai/gpt-oss-20b`), health-checked
independently so it doesn't just inherit whichever model happened to win the
main ensemble slot.

**Dry run** (3 recently-closed MiniBench questions, no publish): all 3
succeeded. Two findings worth flagging:

1. `gemini/gemini-3.8-flash` failed its health check with a transient 503
   ("high demand... please try again later") during this run, so the
   ensemble fell back to `groq/openai/gpt-oss-120b` + `groq/qwen/qwen3.8-27b`
   + an OpenRouter model for all 3 questions. Not a quota issue (503, not
   429) and not permanent -- `gemini-3.5-flash-lite` (the parser pick) was
   healthy the same run, so this looks like transient capacity on that one
   model specifically. The self-healing design did exactly what it's for:
   recorded as DEAD for this run, ensemble proceeded anyway.
2. Real per-provider usage this run: Groq 6 requests (3 each, both models),
   Gemini 4 requests (3 research-summaries + 1 fallback-parse, all via the
   lite parser model), OpenRouter 3 (one per question, read live rather than
   locally tracked). All three providers did answer in some role even though
   gemini-3.8-flash specifically didn't participate as an ensemble member
   this run.
3. Unflagged until now: `cost_usd` is no longer always $0 for Groq/Gemini
   (e.g. $0.0083-0.0159/question this run) -- litellm has real published
   pricing for these well-known model families (unlike the obscure
   OpenRouter :free slugs, which litellm doesn't recognize and so reports
   OpenRouter's own actual-$0 figure for). Since both keys are genuinely
   free-tier with no card on file, **real spend is still $0** regardless of
   what litellm's cost estimator assumes -- but the `cost_usd` column and the
   $ budget guard are now tracking a phantom number for these two providers,
   not real spend. Low practical risk today (the daily question budget caps
   volume well before $10/day in phantom cost could accumulate), but worth
   knowing the $ figures from Groq/Gemini aren't literal going forward.

## Fixed the phantom-cost issue + enabled seasonal, 2026-10-12

**$ guard fix.** `cost_usd` now always means real spend: `metaculus_bot.
provider_quota.split_real_and_estimated_cost` zeroes it out whenever every
model that contributed to a question is a confirmed free-tier-no-card
provider (`is_free_tier_provider`: groq, gemini, or openrouter with a
`:free` suffix), regardless of what litellm's real published pricing says.
litellm's raw number is kept separately as `estimated_cost_usd` (new
column, migrated in via `db._migrate` since `CREATE TABLE IF NOT EXISTS`
doesn't retroactively add columns to the already-persisted data-branch DB).
`budget.price_per_token` got the equivalent fix for the *pre-flight*
estimate. Added `tests/test_cost_accounting.py` (pytest, new dev dependency)
proving a Groq+Gemini-only forecast's real cost is always $0 and the
$/question and $/day guards never block it, even with a deliberately tiny
cap and litellm reporting a large estimate -- 8 tests, all passing.

**Seasonal tournament enabled.** `ENABLE_SEASONAL_TOURNAMENT` defaults
`True` now. New `run_bot_on_seasonal.yaml`, hourly (seasonal questions stay
open for days/weeks, unlike MiniBench's ~3h), sharing an explicit
`metaculus-bot-forecast` concurrency group with `run_bot_on_minibench.yaml`
(previously each used the default `${{ github.workflow }}` group, which is
per-workflow-name and wouldn't have stopped the two from running
simultaneously and racing on the data branch).

**MiniBench quota priority.** `compute_daily_question_budget` takes a new
`reserve` parameter; the seasonal run passes `minibench_quota_reserve`
(default 5) to deliberately leave headroom on the shared Groq/Gemini/
OpenRouter quota rather than spend it down to zero, since MiniBench polls
far more often (15 min vs hourly) against much tighter question windows.

**Seasonal re-forecasting.** `_fetch_one_tournament_with_diagnostics` now
returns `(not_yet_forecasted, already_forecasted)` instead of silently
dropping the latter. For seasonal specifically,
`_select_reforecast_candidates` queries our own `forecasts` table (`db.
get_last_forecast_time`) for anything still open whose last forecast is
`>reforecast_staleness_days` (default 7) old, oldest-first, capped at
`max_reforecasts_per_day` (default 5, tracked across the whole day via `db.
count_reforecasts_today` -- a genuine re-forecast is a forecast with an
earlier successful one for the same question, today or any prior day).
MiniBench keeps the simple drop-if-already-forecasted behavior.

This required decoupling from `ForecastBot`'s own `skip_previously_
forecasted_questions`: that flag's filter runs again inside `forecast_
questions()` and would have silently re-dropped a question we specifically
chose to re-forecast (since Metaculus's `already_forecasted` is still
true). The bot is now always constructed with that flag `False` --
main.py is the sole authority on which questions to attempt, which it
already needed to be for the per-tournament already-forecasted handling.

**Tournament-tagged logging.** New `tournament` column (same migration
mechanism as `estimated_cost_usd`), populated from the mode/target that
produced each question ("seasonal", "minibench", "test", or "manual" for
--post-ids). `scripts/weekly_summary.py` now prints an all-combined section
plus one section per tournament label seen in the window.

**Bug found and fixed while verifying this**: the "closed without being
forecast" alert check ran *before* the forecast loop, so a question that
was already closed at the start of a run (e.g. testing against a known-
closed question via --post-ids) but successfully forecast by that same
run's loop would still trigger a false-positive alert -- it hadn't been
marked forecasted yet at check time. Moved the check (now `_check_
closed_unforecast`) to run after the loop, and to every one of `_run`'s
early-return points, not just the happy path. Verified via a clean re-run:
0 alerts where there were previously 3 false positives.
