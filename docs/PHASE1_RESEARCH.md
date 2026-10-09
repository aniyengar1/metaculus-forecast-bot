# Phase 1 Research — Metaculus FutureEval Bot

Date: 2026-10-04. Status: research only, no bot code written yet.

## 1. The official template (`./template`, cloned from `Metaculus/metac-bot-template`)

### Files
```
.env.template
.github/workflows/{test_bot,run_bot_on_tournament,run_bot_on_metaculus_cup,review_bot}.yaml
.claude/skills/review-bot/SKILL.md
bot_helpers.py
main.py                    # recommended entry point (uses forecasting-tools SDK)
main_with_no_framework.py  # single-file, minimal-dependency reference impl
integrations/main_lightningrod_eval.py
integrations/README.md
pyproject.toml / poetry.lock
```

### How it fetches questions
`main.py` builds a `FallTemplateBot2026(ForecastBot)` (subclass from the `forecasting-tools` package, pinned `>=0.2.90,<0.4.0`) and calls `bot.forecast_on_tournament(tournament_id)`. Internally this hits the Metaculus REST API (`GET /api/posts/` filtered by `tournaments=<id>`, `statuses=open`) and paginates through open questions, skipping ones already forecasted if `skip_previously_forecasted_questions=True`.

The no-framework version (`main_with_no_framework.py`) shows the raw calls directly:
- `GET /api/posts/?tournaments=<id>&statuses=open&forecast_type=binary,multiple_choice,numeric,discrete` → list posts/questions
- `GET /api/posts/{post_id}/` → full question detail (resolution criteria, fine print, bounds, options)
- `POST /api/questions/forecast/` → submit a forecast, body `[{"question": id, "source": "api", **payload}]`
- `POST /api/comments/create/` → post the reasoning as a (private) comment on the question

Auth is a single header: `Authorization: Token <METACULUS_TOKEN>`.

### Question types handled
All of: **binary**, **multiple_choice**, **numeric**, **discrete**, **date**, and **conditional** (parent/child pairs). `main.py` has a dedicated `_run_forecast_on_*` method and prompt per type:
- Binary → single probability ("Probability: ZZ%"), clamped to [0.01, 0.99].
- Multiple choice → per-option probabilities, parsed and renormalized.
- Numeric/date → 6-point percentile elicitation (P10/20/40/60/80/90), converted into a 201-point CDF via `NumericDistribution.get_cdf()` (handles log-scaled/zero-point questions, open vs. closed bounds, a max-PMF-per-bin cap of ~0.2, and minimum spacing rules it will raise on if violated).
- Conditional → forecasts parent and child separately (affirms previous forecast on parent if still valid) and combines into a `ConditionalPrediction`.

Parsing from raw LLM text to structured output is done by a second LLM call (`structure_output`, with 2 validation samples) rather than regex — the no-framework version uses regex instead, which is `main_with_no_framework`'s main weakness as a base to build on.

### How it submits
After `run_research` + `run_forecast`, if `publish_reports_to_metaculus=True` the SDK posts the forecast and reasoning-as-comment. Dry-run is just: set that constructor flag to `False` (main.py) or `SUBMIT_PREDICTION = False` (no-framework version).

### Scheduled runs (GitHub Actions)
Three workflows, all using Poetry + Python 3.11, secrets passed as env vars:
- `test_bot.yaml` — manual dispatch only, targets `bot-testing-area` (`--mode test_questions`). Recommended first smoke test.
- `run_bot_on_tournament.yaml` — cron `7,27,47 * * * *` (every 20 min), `--mode tournament` → forecasts both the seasonal tournament (`client.CURRENT_AI_COMPETITION_ID`) and MiniBench (`client.CURRENT_MINIBENCH_ID`) in the same run.
- `run_bot_on_metaculus_cup.yaml` — cron `0 0 */2 * *` (every 2 days), `--mode metaculus_cup`.
- `review_bot.yaml` — optional, weekly, off by default (gated on repo variable `REVIEW_BOT_ENABLED`), runs the community `metaculus-bot-review` package for post-hoc scoring analysis.

Each run is idempotent (skips already-forecasted questions) and safe to re-trigger; concurrency groups prevent overlapping runs of the same workflow.

### Tournament ID constants (from `forecasting_tools.MetaculusClient`, current as of today)
```python
FE_FALL_2026_ID            = 33121        # fall-futureeval-2026
METACULUS_CUP_FALL_2026_ID = 33108        # metaculus-cup-fall-2026
Q4_2026_MARKET_PULSE_ID    = "market-pulse-26q4"
CURRENT_MINIBENCH_ID       = "minibench"  # always points at whatever 2-week round is live

CURRENT_AI_COMPETITION_ID = FE_FALL_2026_ID
CURRENT_METACULUS_CUP_ID  = METACULUS_CUP_FALL_2026_ID
CURRENT_MARKET_PULSE_ID   = Q4_2026_MARKET_PULSE_ID
```
These `CURRENT_*` aliases are what `main.py` actually uses, so a `forecasting-tools` upgrade each season is what keeps them current — no code change needed in our bot as long as we upgrade the dependency. `BOT_TESTING_AREA_ID = "bot-testing-area"` is the sandbox tournament for smoke tests (contains all question types, forecasting on it doesn't count).

### Notable implementation details worth keeping
- `_structure_output_validation_samples = 2` — the parser LLM call is validated twice before accepting structured output, cheap insurance against malformed parses.
- `_max_concurrent_questions = 1` via `asyncio.Semaphore` — conservative default, tunable based on our rate limits.
- `bot_helpers.py` centralizes env-var validation (`check_environment`) and run-summary banners; it refuses to run (exit 1) if `METACULUS_TOKEN` is a placeholder or missing.
- `main_with_no_framework.py`'s `NumericDistribution` class is a fully worked reference for the CDF math (standardization, bound-pinning, max-PMF-per-bin capping) — useful to read even though we'll use the SDK's version.

## 2. Best-performing open-source bots

Metaculus maintains a live, crowdsourced list on the [AI benchmark resources notebook](https://www.metaculus.com/notebooks/38928/ai-benchmark-resources/#open-source-bots). Top performers from recent seasons relevant to our design:

| Bot | Repo | Placement |
|---|---|---|
| **nostreambot** | github.com/No-Stream/nostreambot-metaculus-bot | 9th Fall 2025 (top open-source), 10th–15th Spring/Summer 2026 |
| Metaculus Template Bot (baseline) | Metaculus/metac-bot-template | 17th Spring 2026 |
| Panshul42 | github.com/Panshul42/Forecasting_Bot_Q2 | **1st**, Q2 2025 (originated the PCHIP numeric-smoothing trick nostreambot later adopted) |
| joy.void.joy-bot | github.com/joy-void-joy/aib-joy-void-joy-bot | 20th Spring 2026 |
| alekthebearbot | github.com/alekthebear/castor | 26th Spring 2026 |

### nostreambot — deep dive (the one the user flagged)
Repo: `No-Stream/nostreambot-metaculus-bot`, MIT-licensed, built on `forecasting-tools`, same GitHub Actions deployment model as the template but running hourly plus an external cron-job dispatcher (because GitHub Actions' own schedule trigger is unreliable at exact times — worth copying).

Architecture (`metaculus_bot/forecaster.py`, `metaculus_bot/llm_configs.py`):
- **3-model frontier ensemble**, one per vendor, run in parallel via OpenRouter: latest OpenAI, latest Anthropic, latest Google flagship model, each called with high/xhigh reasoning effort, 64k max tokens, `temperature=None`, `timeout=480s`, `allowed_tries=1`. Separate smaller/cheaper models (`gpt-...-luna`, low effort) are used for parsing, summarizing, and a disagreement-analysis pass — i.e. they don't waste frontier-model budget on mechanical tasks.
- **Aggregation: pointwise median across the 3 models.** For binary this is just the median probability. For numeric/date, each model's elicited percentiles are first turned into a full distribution, then the median is taken pointwise across distributions (not a median of raw percentile values) before converting to Metaculus's 201-point CDF format.
- A "stacker" model that re-reads all 3 forecasts and tries to resolve disagreement exists in code but is **disabled in production** — their own testing on 88 questions showed no improvement over plain median. Useful negative result: don't bother building a meta-forecaster without strong evidence it helps.
- **Numeric questions use PCHIP (monotone cubic Hermite) interpolation** to go from 6-ish elicited percentiles to a smooth 201-point CDF, rather than Metaculus SDK's default linear interpolation between percentiles. This is explicitly credited as borrowed from Panshul42 (the Q2 2025 #1 bot). It avoids the "jagged CDF" artifacts linear interpolation produces and better respects the max-per-bin PMF cap.
- **Research is multi-source and parallel, not just AskNews**: AskNews (primary), OpenAI/Gemini native web search, yfinance + FRED for financial/economic series, and direct price snapshots from prediction markets (Polymarket, Kalshi, Manifold, PredictIt) when a question maps to a live market. Plus a "resolution source" fetcher that pulls the specific page the question's resolution criteria names, and two bounded "gap-fill" agentic passes that find and patch missing facts before forecasting.
- Reported cost: **~$2.60/question** in API spend — useful for budgeting our own credit usage.
- Supports forecasting on Metaculus and a second platform ("Mantic Crucible") from the same codebase — not relevant to us initially.

**Takeaway for our design**: median-of-3-frontier-models + PCHIP smoothing for numeric is a proven, replicable pattern (it's literally what placed 1st and top-10 in multiple seasons) and matches what the user already proposed. Multi-source parallel research and the disabled stacker are both worth noting as "tried, marginal/no benefit" data points — we should not over-invest in a stacker without testing against our own resolved questions first.

## 3. Current FutureEval rules & resources (as of 2026-10-04)

Source: `metac-bot-template` README, Metaculus AI-benchmark-resources notebook (#38928), and web search of Metaculus/EA-forum tournament announcements. (`metaculus.com/futureeval/participate/` and `.../futureeval/methodology/` both returned HTTP 403 to automated fetches — likely bot-blocking on those specific pages — so rules below are corroborated via the README + search snippets + forum announcements rather than a direct fetch. Worth opening those two URLs yourself in a browser before relying on edge-case rule details.)

### Submission requirements / rules
- **No human in the loop**: no previewing your bot's forecast on an open/upcoming question and then tuning it, no re-running because you dislike the output, no copy/paste submission. Testing is only allowed against closed tournament questions or the `bot-testing-area` sandbox.
- Each forecast must come with a **comment explaining the reasoning** (the template does this automatically — posted as a private comment via `/api/comments/create/`).
- **One prize-eligible bot per participant/team.** Secondary/experimental bots are allowed but must be labeled (`v2` etc. in bot username and its account email) and are not prize-eligible.
- **Prize winners must submit code or a written architecture description** and accept a Metaculus inspection — i.e. if we place well, be ready to either open-source or document the bot in detail.
- Using publicly available forecasts from other platforms/Metaculus itself as an input signal is explicitly allowed.
- All participants must fill out the (short, 3-question) [participation form](https://forms.gle/aQdYMq9Pisrf1v7d8) before submitting forecasts — this form doubles as the LLM-credit application.

### Prize structure (confirms the user's framing)
Spot peer score is log-based, scored relative to how hard the question was for other forecasters. **Prize share is proportional to the sum of each bot's positive peer scores, squared** — the squaring is the important nonlinearity: being clearly above average is disproportionately rewarded, and below-average (non-positive sum) bots get $0. This matches "finish above average, then push for top 10" as the right two-stage goal, since the squaring means the jump from "above average" to "top 10" is where the real prize money is.

### Tournament structure / current slugs
- **Fall 2026 seasonal** — `fall-futureeval-2026` / numeric ID `33121`. $50k pool, ~300–500 questions, runs 2026-09-28 → 2027-01-06. One of 3 seasons/year aligned to the Metaculus Cup.
- **MiniBench** — slug `minibench` (this alias always points at whichever 2-week, ~60-question, $1k round is currently live — no need to hardcode a round-specific ID).
- **Market Pulse Q4 2026** — slug `market-pulse-26q4`, confirmed to exist as a live tournament constant in `forecasting_tools` already (so it's either live or imminently so). Historical Market Pulse pool has been ~$7k and was binary-only in Q3/Q4 2024, expanding to numeric/MC by Q1/Q2 2025 — assume all types possible for Q4 2026 and let the bot's type-dispatch handle whatever shows up.
- **Metaculus Cup Fall 2026** — slug `metaculus-cup-fall-2026` / ID `33108`. Separate cadence (every 2 days, not every 20 min) since it's lower-volume/exhibition.
- **bot-testing-area** — permanent sandbox, all question types, forecasts here don't count and aren't rate-limited the same way — this is where `test_bot.yaml` points and where we should do all local iteration.

### LLM credits & AskNews (per-season application required)
- **LLM credits**: apply via the same [participation form](https://forms.gle/aQdYMq9Pisrf1v7d8); Metaculus distributes credit via an **OpenRouter key** covering OpenAI/Anthropic/Google models. Check remaining balance via OpenRouter's API `limit_remaining` field. Explicitly told: "if you run out, assume we won't be able to give you more" — so we should budget per-question spend (nostreambot's ~$2.60/question is a reasonable reference point) and build in a cutoff/alert rather than relying on being topped up.
- **AskNews**: separate signup at `my.asknews.app` using the bot's email, then contact AskNews (Discord/DM/email `contact@asknews.app`) with bot name, registered email, name, LinkedIn, and affiliation to get it provisioned for the tournament. Generate `ASKNEWS_API_KEY` (or `ASKNEWS_CLIENT_ID`/`ASKNEWS_SECRET` pair — template supports both auth styles) at `my.asknews.app/en/settings/api-credentials`.
- **AskNews rate limits**: **1,000 calls/month, 4,000 calls total for the tournament**, 5M token budget. Within that: a "latest news" (`/news`, 48h-back) call costs 1 unit, an "archive"/historical-knowledge call costs 5 units — so broad historical queries are 5x more expensive than a recent-news query. This should directly shape our research-step design (favor targeted recent-news calls, use archive/deep-research sparingly).
- Both credit pools are **per-season** — must re-apply/renew for Fall 2026 specifically even if previously granted for Spring/Summer.

## 4. Accounts, tokens, and env vars needed from you

Required to get a working dry-run going at all:
| What | Env var | Where to get it |
|---|---|---|
| Metaculus **bot account** (separate from your human account) + its API token | `METACULUS_TOKEN` | Human account → Settings → "My Forecasting Bots" → "Create a Bot" → copy API key |
| At least one LLM provider key | `OPENROUTER_API_KEY` (recommended — one key, multi-vendor) or `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | OpenRouter free tournament credits via the [participation form](https://forms.gle/aQdYMq9Pisrf1v7d8) (apply as bot-maker), or buy your own key |

Required before the Fall 2026 season counts / before scaling up research quality:
| What | Env var | Where to get it |
|---|---|---|
| Participation form (counts as registration + credit application) | n/a (web form) | https://forms.gle/aQdYMq9Pisrf1v7d8 — do this first, it's a prerequisite for the credits above |
| AskNews credentials | `ASKNEWS_CLIENT_ID` + `ASKNEWS_SECRET` (or `ASKNEWS_API_KEY`) | Sign up at my.asknews.app with the bot's email, then message AskNews per the resources page to get tournament access provisioned |

Optional, for the "one more search source if credits allow" step in Phase 2:
| What | Env var |
|---|---|
| Perplexity | `PERPLEXITY_API_KEY` |
| Exa | `EXA_API_KEY` |

Not needed unless we adopt that integration:
| What | Env var |
|---|---|
| LightningRod SDK (synthetic question generation for calibration practice) | `LIGHTNINGROD_API_KEY` |

All of the above go in GitHub Actions → repo Settings → Secrets and variables → Actions → New repository secret (names must match exactly), and/or a local `.env` copied from `.env.template` for local dev — never committed.

## Open items / things to verify yourself in a browser
- `futureeval/participate/` and `futureeval/methodology/` 403'd on automated fetch — worth a manual look for any fine print not captured in search snippets (e.g. exact Market Pulse Q4 dates/pool size, any 2026-specific rule changes).
- AskNews provisioning requires a human conversation (Discord/DM/email) — start that early since it's not self-serve.
- Confirm your bot account's email before messaging AskNews, since they ask for "registered email" as part of verification.
