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

## Seasonal cadence correction, 2026-10-12

After the first live seasonal forecast landed, pulled all 27
`fall-futureeval-2026` posts directly from the API, unfiltered (one of the
28 posts returned is a non-question announcement notebook, excluded). Full
breakdown:

- **1 pre-season practice question** (`[PRACTICE] ...`): open Sep 6 → Sep
  28, a 528-hour (~22 day) window, already resolved. Not representative of
  the tournament proper.
- **26 real questions**, every single one Sep 28 → Oct 12: **exactly a
  3.0-hour open window, zero exceptions** -- identical to MiniBench's
  format, not the "days/weeks" originally assumed when the seasonal
  schedule was built hourly. 25 closed, 1 open (the gas-price question from
  the first live run).
- **Cadence**: 1.82 questions/day average over the 14.3-day span so far, but
  bursty -- 0-4/day, including a 4-day gap (Oct 2-4) and a 4-per-day stretch
  (Oct 10). Median gap between consecutive opens: 10.0h; max: 99.3h.

**Fix**: `run_bot_on_seasonal.yaml` moved from hourly to every 15 min, same
as MiniBench, since a 3h window needs frequent polling to reliably catch.
Offset by 7 min from MiniBench's `:00/:15/:30/:45` (`:07/:22/:37/:52`) so
the two interleave on their shared concurrency group rather than colliding
on literally every trigger.

**Re-forecasting, confirmed cheap even though it's now mostly moot**: with
3h windows, a question is closed long before any realistic staleness
threshold (default 7 days) could apply while it's still open -- so
`_select_reforecast_candidates` will almost always see an empty candidate
list for this tournament's current format. Confirmed it costs nothing in
that empty case regardless: it operates on `already_forecasted`, which is
already fetched as part of the single per-tournament API pull (no extra
Metaculus request), and the only other work is two local SQLite queries
(`get_last_forecast_time`, `count_reforecasts_today`) -- no LLM calls happen
unless a genuine stale-and-still-open candidate actually exists. Kept as-is
rather than removed: cheap insurance if the tournament's format changes to
include longer-lived questions later in the season (the tournament runs
through Jan 6, 2027, and nothing guarantees every batch looks like the
first ~2 weeks).

## Comment privacy and length, 2026-10-13

Metaculus treats long *public* bot comments as spam, with penalties up to
account deactivation; private comments are tolerated "within reason."
Checked both the `forecasting_tools` source and the live API:

- **Already private**: `MetaculusClient.post_question_comment` defaults to
  `is_private=True`, and none of the three report types'
  `publish_report_to_metaculus()` override it. Confirmed empirically against
  all 8 comments posted so far (posts 45960, 45961, 43324, 43325 x2, 43326,
  43327, 45517): 8/8 private, 0 public. Nothing to clean up.
- **But far too long**: those 8 comments ran 6,293-11,506 characters each --
  the base class's default `_create_unified_explanation` dumps the full
  research text and every model's complete reasoning. Long either way isn't
  "within reason," privacy aside.

**Fix**, in `metaculus_bot/bot.py`:
- `FutureEvalBot._create_unified_explanation` overrides the base class to
  build a short comment instead: final prediction + up to 5 per-model
  bullets (via the new `_short_value_repr` helper, which renders a
  `NumericDistribution`'s median instead of its full ~thousands-of-chars
  repr) + cost/time. Hard-capped at `_MAX_COMMENT_CHARS` (1500).
- `_force_private_and_cap_length` instance-patches
  `self.metaculus_client.post_question_comment` (patched on the instance,
  not the class, so it can't affect any other code sharing the SDK) to
  force `is_private=True` and re-apply the length cap at the actual
  API-call layer too -- belt-and-suspenders in case any future caller
  bypasses `_create_unified_explanation`.

Covered by `tests/test_comment_safety.py`.

## Performance report, 2026-10-13

`scripts/performance_report.py`, run as part of the weekly summary
(`scripts/weekly_summary.py`), pulls Metaculus's own score for every
question we've forecast that has since resolved and combines it with our
local log. Read-only: every call is a GET against `/api/posts/{id}/with_cp=true`,
never a submission.

**Finding the score schema was the hard part** -- the REST API has no
published docs for it, and this environment doesn't yet have any resolved
*and scored* question of ours to inspect empirically (checked: of our own
forecasted test/tournament questions, none are `resolved` yet as of this
writing; general site-wide `resolved` questions sampled also had null
`resolution`/`score_data`, so this Metaculus instance's scoring batch job
plausibly hasn't run yet either). Found the real shape instead via
Metaculus's open-source webapp repo (`Metaculus/metaculus` on GitHub),
`docs/openapi.yml`: a resolved+scored question's
`question.my_forecasts.score_data` (our own score) and
`question.aggregations.<method>.score_data` (community's) both look like:

```
{"peer_score": 51.67, "coverage": 0.998, "baseline_score": 35.15,
 "spot_peer_score": 57.56, "peer_archived_score": ..., ...}
```

MiniBench and Market Pulse score with `spot_peer_score`/`spot_baseline_score`;
the seasonal tournament's `score_type` is non-spot `peer`/`baseline`. The
report prefers the non-spot key and falls back to the spot one, so either
tournament type yields one number.

**What it reports**:
- Avg peer/baseline score overall, by tournament, by question type -- only
  over questions we actually *submitted* (`dry_run=0`); Metaculus never
  scores a forecast that was never sent.
- Per-model Brier for binary questions: each model's raw, pre-ensemble
  probability vs. the actual "yes"/"no" outcome, parsed from
  `raw_model_outputs` in our own log.
- Raw ensemble Brier vs. post-calibration Brier (binary only -- calibration
  is a binary-only hook, see `metaculus_bot/calibration.py`), plus the
  average gap between our submitted probability and the community
  prediction *at the time we forecast* (from the CP history timeseries in
  `aggregations.<method>.history`, matched to our `run_timestamp`).
- A 10-bin calibration table (predicted probability vs. empirical resolution
  rate), binary only, including dry-run forecasts (that part only needs our
  predicted probability + the eventual outcome, not an official submission).

**A real local-DB quirk found and worked around**: `aggregate_value` and
`post_calibration_value` are stored *double JSON-encoded* -- `bot.py`'s
`_record_debug` already does `str(value)` before handing it to
`db.log_forecast`, which then does its own `json.dumps(..., default=str)` on
top, so the column literally contains `"0.42"` with the quote characters
included. `performance_report._parse_logged_value` does `json.loads()` once
to undo that before `float()`-parsing.

Covered by `tests/test_performance_report.py` (score_data extraction + spot
fallback, CP-history lookup, Brier/calibration math, and an end-to-end
`generate_report()` test with `fetch_question_json` monkeypatched -- no real
network call in tests).

## Date/numeric aggregation bug, 2026-10-16/17

Found via the upcoming-resolutions table above: post 43324 ("When will
martial law be lifted in at least 3/4 of Ukraine?") had a submitted median
resolution date of **2024-11-26** -- before the question even opened
(2026-04-27).

**Root cause**: `build_pchip_numeric_distribution`
(`metaculus_bot/aggregation.py`) fit a PCHIP interpolator to each model's
own sparse elicited percentiles (e.g. 6 points spanning only that model's
believed P10-P90 window -- for 43324, roughly a 6-18 month span), then
evaluated that fit at a 201-point grid spanning the question's *entire*
[lower_bound, upper_bound] range (for 43324, ~5 years). `PchipInterpolator`
with `extrapolate=True` is only well-behaved *inside* its fitted domain;
asked to extrapolate years beyond it, the cubic can swing wildly.
Reproduced empirically with the question's real bounds and real elicited
percentiles: raw extrapolated values of **600%+ "probability"** at the
far (2022) end of the grid. `np.clip(0, 1)` clamps that to 1.0, and
`np.maximum.accumulate` right after then locks that 1.0 in as a floor for
the entire rest of the (chronologically later) curve -- corrupting the
median. Reproduced the exact incident class using the real submitted
per-model percentiles for 43324: buggy aggregate median came out to
2024-05-03.

**Fix**: before fitting PCHIP, extend the elicited percentiles with
explicit bound anchors via `NumericDistribution`'s own
`_add_explicit_upper_lower_bound_percentiles` -- the same private helper
the SDK's own default (linear) CDF builder already relies on internally
(`get_cdf` -> `_get_cdf_at`). This way the PCHIP fit's domain already
covers the full grid and little or no extrapolation is needed. Verified:
same real data now produces a median of 2027-05-11, consistent with both
models' own elicited windows. Applies to `NumericQuestion`,
`DiscreteQuestion` (a `NumericQuestion` subclass, same code path), and
`DateQuestion` alike. Covered by `tests/test_aggregation.py`, including a
test that fails without the fix (confirmed by reverting it) and passes
with it.

**Second, independent line of defense**: `metaculus_bot/sanity_guard.py`'s
`check_numeric_or_date_distribution`, called from
`FutureEvalBot._check_distribution_or_raise` right after aggregation in
both `_run_forecast_on_numeric` and `_run_forecast_on_date`, before
`_record_debug`/submission. Checks the final median/P10/P90 against the
question's own bounds (a backstop -- the value axis should always be
in-bounds by construction, so this only fires if some *other* bug breaks
that invariant) and, for date questions, that the median is after "now".
On failure: logs an alert (`db.log_alert`), logs a
`status="skipped_invalid_distribution"` forecast row, and raises
`InvalidDistributionSkipped` -- caught in `main.py` exactly like the
existing `BudgetSkipped` pattern, so the question is skipped rather than
submitted. Covered by `tests/test_sanity_guard.py` (the pure check) and
`tests/test_bot_sanity_guard_wiring.py` (the bot-level wiring, using a
lightweight stand-in for `self` rather than a full `ForecastBot`).

**Audit of all 6 already-submitted numeric/date/discrete forecasts**
(post_calibration_value re-parsed, each model's raw elicitation re-parsed
from `raw_model_outputs` and re-aggregated with the fixed code, and the
new guard run against what was actually submitted):

| Post | Type | Status | Submitted median | Fixed-recompute median | Guard |
|---|---|---|---|---|---|
| 43324 | date | open | 2024-11-26 | 2027-05-11 | **FAIL** |
| 45517 | discrete | open | 0.455 | 0.372 | pass |
| 45960 | numeric | closed | 4.125 | 4.372 | pass |
| 45961 | numeric | closed | 1.175e+08 | 1.168e+08 | pass |
| 45965 | discrete | closed | 8.5 | 9.497 | pass |
| 46052 | discrete | closed | 5.786 | 3.52 | pass |

Only 43324 fails the guard and is still open -- re-forecast with the fixed
code (real submission, replacing the bad standing forecast). The other 5
show the expected smaller shift from the bound-anchoring fix but were
never corrupted the way 43324 was; the 4 already-closed ones can't be
re-forecast (and don't need to be -- they pass the guard).

## Follow-up, 2026-10-18: re-audit of all still-open questions + retry confirmation

**43324 re-forecast, confirmed live**: submitted median moved from
2024-11-26 (bad) to 2027-03-19 (sane, after both now and the question's
open_time), P10=2026-08-10, P90=2027-05-28 -- passes the guard. The forced
re-forecast required `--mode test_questions` (the only way `--post-ids`
bypasses the already-forecasted safety net in `main.py`), which records
`tournament="test"` locally -- cosmetic bookkeeping only, doesn't affect
the real Metaculus submission. That run was local (not a GitHub Actions
job), so its log row never reached the shared `data` branch automatically
-- merged it in by hand (attached the local db, copied just that one row
by `post_id`+`run_timestamp`, verified the row count went from 28 to 29
with no duplication) and pushed via the existing
`scripts/persist_data_branch.sh`.

**Re-audit of both still-open numeric/discrete/date questions**
(recomputing from each model's actual `raw_model_outputs.raw_text`,
re-parsed through the production parsers and the fixed
`build_pchip_numeric_distribution`, compared against the submitted CDF
pulled live):

| Post | Submitted median | Fixed-recompute median | Max CDF diff | Note |
|---|---|---|---|---|
| 43324 | 2027-03-19 | 2027-05-28 (unreliable, see below) | 0.37 (unreliable) | Already re-forecast above; live-API value is authoritative |
| 45517 | 0.373 | 0.372 | 0.048 | Negligible -- no action needed |

43324's "recompute" in this re-audit is **not a reliable comparison** and
should be disregarded: 2 of the original run's 3 models couldn't be
re-parsed from their stored `raw_model_outputs.raw_text` --
`groq/openai/gpt-oss-120b` legitimately used the LLM-fallback parser
originally (consistent, not a bug), but `groq/qwen/qwen3.8-27b` genuinely
*could* have been re-parsed except that `_record_debug` truncates
`raw_text` to 4000 characters for storage, and this model's answer block
happened to fall past that cutoff. The recompute above is therefore built
from only 1 of 3 original models and isn't apples-to-apples -- the
authoritative check for 43324 is the live-API-confirmed submitted value
above, independently verified to pass the guard. (The 4000-char truncation
is a pre-existing storage limit, not addressed here -- it only affects
retrospective audits like this one, not the original live run, which
always parses from the full untruncated text in memory.)

45517's recompute is reliable (all 3 models cleanly re-parsed, no
truncation hit) and shows a negligible difference -- it was never
corrupted and doesn't need re-forecasting, consistent with it passing the
guard both before and after.

**Retry-on-429/5xx confirmed already in place for the main bot**: traced
`forecasting_tools`' `MetaculusClient` (the SDK `bot.py`/`main.py` use
directly for all Metaculus calls -- no direct `requests.get/post` to
Metaculus anywhere in our own bot code). `get_question_by_post_id`,
`_get_questions_from_api` (underlies
`get_all_open_questions_from_tournament`/`get_questions_matching_filter`,
i.e. all our question fetching), `post_question_comment`, and
`_post_question_prediction` (underlies all three
`post_*_question_prediction` submission methods) are each wrapped in the
SDK's own `retry_with_exponential_backoff()` -- up to 3 retries, which
catches `requests.exceptions.RequestException` (the base class
`HTTPError` derives from, so 429/5xx responses are covered, along with
connection/timeout errors). No code change needed here. Added
`tests/test_metaculus_retry.py` to prove this *behaviorally* (mocking a
429/503 then a successful response, for both a fetch and two submission
paths) rather than just asserting a decorator is present, so a future
`forecasting_tools` version silently dropping this would be caught by our
own test suite. The only other direct-to-Metaculus HTTP calls in this repo
are in `scripts/performance_report.py`'s `fetch_question_json` (used by
both the performance report and the upcoming-resolutions report), which
already got the same retry treatment in an earlier round -- see
"Performance report" above.

## raw_text truncation + structured parsed_value, 2026-10-18/19

The 4000-char `raw_text` cap (`_record_debug` in `metaculus_bot/bot.py`)
was found, while auditing the date-aggregation bug above, to cut off a
real model's answer block entirely before it reached the actual
percentile lines -- meaning retrospective analysis that needs to
re-derive a value from `raw_text` can silently lose data, and the
existing `parsed_value` field was just `str(r.value)` (a Python repr --
exact for a model's own analysis but not valid JSON for anything except
binary, so multiple-choice/numeric/date values needed fragile
regex-from-repr parsing, as several scripts in this session already had
to do).

**Fix 1 -- store each model's parsed value cleanly, separately from
raw_text**: `ModelRunResult` gained a `parsed_percentiles` field (the
model's own *sparse* elicited percentiles, e.g. 6 points, before PCHIP
expansion), threaded through `_single_numeric_run`/`_single_date_run`.
`_record_debug` now calls a new `_structured_parsed_value(result)`
instead of `str(r.value)`: a plain float for binary, `{option: prob}` for
multiple choice, and `[{"percentile": p, "value": v}, ...]` (the sparse
elicitation, not the ~201-point expanded CDF) for numeric/discrete/date.
All directly JSON-usable -- no re-parsing needed by any later analysis
script. Covered by `tests/test_structured_parsed_value.py`.

**Fix 2 -- store raw_text (near-)untruncated**: cap raised from 4000 to
`_MAX_RAW_TEXT_CHARS = 20_000`. Measured real full-length raw_text across
this session's live runs and the full production history (76 model-run
entries): average 2308 chars, max 4000 (i.e. every sample at or near the
old cap *was* the old cap -- genuinely truncated), only 14.5% of entries
ever hit it. 20,000 is ~5x the old cap and ~9x the observed average --
functionally untruncated in practice while still bounding a pathological
runaway/repeating response. No compression needed (see size measurement
below) -- plain text, same as before, just a higher cap.

**DB size impact, measured (not estimated) against the real production
`data/forecasts.db`**:
- Current file size: 639 KB (29 rows, 27 with model outputs, 76
  individual model-run entries after the backfill below).
- `raw_model_outputs` is 32% of the file (206 KB); the new `parsed_value`
  field itself adds under 0.2% (11 bytes out of a 7.4 KB entry, measured
  on a real binary question) -- raw_text dominates, as expected.
- Removing the 4000-char cap: of the 76 real entries, only 11 (14.5%)
  were ever at the cap; even assuming a generous 2x overshoot for all of
  them if uncapped, that's roughly +44 KB one-time catch-up on the
  existing history -- under 7% of current file size.
- **The bigger size driver, measured directly, isn't the cap at all --
  it's the git-branch-as-storage architecture itself**: `scripts/
  persist_data_branch.sh` commits a full new blob of the whole db file on
  every run that changes it (SQLite's on-disk format doesn't diff well
  byte-for-byte even for a one-row change, so git can't delta-compress
  these effectively). Measured locally: 29 commits on the `data` branch
  over ~10 days have added 5.6 MB of (loose, unpacked) git objects across
  both branches combined -- roughly 560 KB/day at the current ~3
  real-commits/day rate. Extrapolated linearly across the ~75 days
  remaining in the tournament (through 2027-01-06): **~40-50 MB total**
  git history by the end, even as the db itself keeps growing. This is
  comfortably within normal repo size (GitHub's practical concerns start
  in the multi-GB range) and matches `restore_data_branch.sh`'s own
  original call: "a slowly-growing binary blob in git history... a
  non-issue at this scale." No action taken here -- flagging the number
  because it was asked for, not because it needs fixing.

**Fix 3 -- backfill existing rows**: `scripts/backfill_parsed_values.py`
migrates every existing row's old-format `parsed_value` (the `str(value)`
repr) to the new clean format -- deliberately extracted from that
*existing* repr string, not by re-parsing `raw_text`. Key realization:
the repr was always built from the full in-memory value, before any
truncation was ever applied (truncation only ever hit `raw_text`, a
separate dict key) -- so backfill via the repr recovers data the
raw_text re-parsing approach (used elsewhere in this session's earlier
audits) could not, with zero raw_text dependency at all.
  - Binary: the repr is already a plain float string -- exact, lossless.
  - Multiple choice: regex-extracts `option_name=.../probability=...`
    pairs from the `PredictedOptionList` repr -- exact, lossless.
  - Numeric/discrete/date: regex-extracts `Percentile(percentile=..,
    value=..)` pairs from the `NumericDistribution` repr. This is the
    already-built ~201-point expanded CDF, not the model's original
    sparse elicitation (not recoverable for historical rows without
    `raw_text`, which may itself be truncated) -- backfilled rows
    therefore store 3 *representative* percentiles (closest match to
    10/50/90) from that expanded distribution, a coarser provenance than
    what new rows store going forward. Documented in the script.
  - **Result, run against the real production db**: 27 rows scanned, all
    27 changed, 76 entries migrated, 0 failures, 5 entries correctly left
    `None` (the model run itself had errored -- nothing to recover).
    Final db size: 639 KB (was 631 KB -- the clean JSON format is
    actually smaller than the verbose Python repr it replaced, despite
    adding the new structured field). Pushed to the `data` branch via the
    existing `scripts/persist_data_branch.sh`. Covered by
    `tests/test_backfill_parsed_values.py`, including an idempotency
    test (safe to re-run against already-migrated rows).

## Both workflows failing, 2026-10-19 -- rate-limit guard misclassified backup-poller runs

Both `run_bot_on_minibench.yaml` and `run_bot_on_seasonal.yaml` started
failing on every `workflow_dispatch`-triggered run starting ~07:05-07:10
UTC. **Not** the Ubuntu 24.04 -> 26.04 `ubuntu-latest` migration (the
initial hypothesis) -- checked the actual failed runs' "Set up job" step:
`Image: ubuntu-24.04`, still the old image at the time of failure, and
every environment-setup step (checkout, Python, poetry install) succeeded
cleanly in every failed run inspected.

**Actual cause**: `main.py`'s pre-flight OpenRouter quota guard
(`_check_rate_limit_guard`) exists to stop local/manual runs from eating
quota reserved for the next scheduled production run. It exempted itself
only when `GITHUB_EVENT_NAME == "schedule"` -- correct back when
`workflow_dispatch` only ever meant a human manually testing via the GH
UI. The GCP VM backup poller (added because GitHub's native `schedule:`
trigger was silently skipping ~96% of ticks -- see "Backup poller" above)
also fires via `workflow_dispatch`, every ~10 minutes. Once OpenRouter
free-tier quota dipped below the 15-request reserve threshold partway
through the day (used increased from normal traffic), the guard started
refusing every one of *those* dispatch-triggered runs -- which by then
were the actual production traffic, not local testing -- while the
handful of remaining native `schedule:` ticks kept succeeding right
alongside the failures (confirmed in `gh run list`'s `event` column: every
failure is `workflow_dispatch`, every success in the same window is
`schedule`).

**Fix**: `IS_CI_RUN` (renamed from `IS_SCHEDULED_RUN`) now checks
`GITHUB_ACTIONS == "true"` instead of `GITHUB_EVENT_NAME == "schedule"`.
`GITHUB_ACTIONS` is set to `"true"` for every GitHub Actions run
regardless of which trigger fired it (schedule or workflow_dispatch) and
unset when run locally -- the real distinction this guard needs is
"running in CI" vs "someone's laptop," not which specific trigger fired
it. Covered by `tests/test_rate_limit_guard.py`, including the exact
regression (a `workflow_dispatch`-event CI run must be exempt) and
confirming local runs are still correctly guarded.

**Impact**: both workflows failed on every `workflow_dispatch` tick for
roughly an hour (~07:05-08:10 UTC) before this was caught and fixed;
native `schedule:` ticks kept working throughout, so coverage wasn't a
total outage, just reduced back toward GitHub's already-known-unreliable
cadence for that window. See the post-fix verification run further down
for exact failure counts and whether any question's window was missed.
