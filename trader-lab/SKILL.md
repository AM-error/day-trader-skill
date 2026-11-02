---
name: trader-lab
description: >-
  Strategy Lab. A controlled self-improvement loop for the opening-range
  breakout: freezes 60 days of 5-minute bars for a list of stocks, splits the
  dates into development, validation and a locked holdout, then runs
  observe, diagnose, hypothesize, test, keep or revert, log. Claude reads
  where the champion rule loses (development dates only), proposes ONE
  change as a JSON spec, and the lab scores it net of costs against five
  checks: enough trades, beats the champion by more than a noise bar that
  rises with every experiment, survives twice the costs, holds across time
  windows, and survives nudging its numbers. Only a candidate that passes
  every check can be promoted. Every experiment is logged, kept or not. The
  champion hands its rule to trader-entry and trader-exit. Use when the user
  says "improve my strategy", "backtest the opening range breakout", "make
  the bot self-improve", "test this idea", "walk forward", "is this filter
  real", "research a strategy", "tune the setup", or before trusting a rule
  for /trader-entry.
argument-hint: "[fetch | init | diagnose | test SPEC | promote ID | log | show | holdout | forward | check TICKER]"
---

# trader-lab

The Strategy Lab is the research step that comes before the day-trade chain.
The other skills take one trade at a time. The lab asks whether the RULE
behind those trades earns anything after costs, and it lets Claude try to
improve the rule without fooling itself. Most ideas fail here, and that is the
lab doing its job. Tested on enough ideas, past data always produces a
winner by luck. The lab's checks are there to catch that. Most people who day
trade lose money, and a backtest does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/lab.py" fetch --symbols TSLA,NVDA,AMD,AAPL,META,AMZN,MSFT,GOOGL
python3 "${CLAUDE_SKILL_DIR}/lab.py" init                       # champion v1 = the reel's rule
python3 "${CLAUDE_SKILL_DIR}/lab.py" diagnose                   # where it loses, development dates only
python3 "${CLAUDE_SKILL_DIR}/lab.py" test trader/lab/candidate.json --note "one-line hypothesis"
python3 "${CLAUDE_SKILL_DIR}/lab.py" promote 3                  # only a candidate that passed every check
python3 "${CLAUDE_SKILL_DIR}/lab.py" log                        # every experiment, kept or not
python3 "${CLAUDE_SKILL_DIR}/lab.py" show                       # the champion and its hand-off commands
python3 "${CLAUDE_SKILL_DIR}/lab.py" holdout                    # once per champion version
python3 "${CLAUDE_SKILL_DIR}/lab.py" forward                    # sessions after the snapshot: the real test
python3 "${CLAUDE_SKILL_DIR}/lab.py" check TSLA                 # today's session against the champion's filters
```

Other flags: `init --min-trades 20 --slippage-bps 3 --spec FILE --force`,
`--json` on diagnose, test, log and show, `--offline PATH` on fetch, forward
and check (saved Yahoo chart JSON, or a folder of `<SYM>-60d5m.json(.gz)`),
`--now "2026-10-19 10:15"` (New York time) on check, forward and init.

## What it reads and writes

| file | what |
| --- | --- |
| Yahoo chart API | `fetch` and `forward` only: 60 days of 5-minute bars per symbol, free and unofficial |
| `trader/lab/data/` | the frozen snapshot, `<SYM>-60d5m.json.gz`. Every test reads these, so results repeat |
| `trader/lab/lab.json` | symbols, dates, the split, costs, min trades and every champion version with its holdout result |
| `trader/lab/experiments.jsonl` | one line per test: the spec, the hypothesis, every score and every check |
| `trader/lab/candidate.json` | where Claude writes the next spec (any path works) |

## The spec

A spec is the rule as JSON. Leave out a field to keep the reel's value.

| field | reel | what |
| --- | --- | --- |
| `or_minutes` | 15 | opening range, 5 to 60 in steps of 5 |
| `stop_rule` | `or_mid` | `or_mid`, `or_low` or `pct` (with `stop_pct`) |
| `target_r` | 1.0 | target = entry + R x risk |
| `no_entries_before` / `no_entries_after` | 09:45 / 15:30 | the entry window |
| `flat_by` | 15:55 | out by then, win or lose |
| `min_or_pct` / `max_or_pct` | none | opening-range width as % of price |
| `min_gap_pct` / `max_gap_pct` | none | the open vs the previous close, % |
| `min_breakout_vol` | none | breakout bar volume / opening-range average volume |
| `above_prev_close` | false | only enter above the previous close |

Plus `name` and an optional `note` (the hypothesis). Long only, one trade per
symbol per day, first breakout only.

## How it scores

Entry, stop and target follow trader-entry and trader-exit. The stop is
checked first in every bar, and a bar that opens through the stop fills at
its open. Costs are 3 basis points of slippage per market fill by default
(the entry, a stop and a close at `flat_by`; a target fills at its limit),
plus any commission per share. R is net of costs.

The dates (across every symbol) split 60 / 20 / 20:

- **development**: `diagnose` reads only these. Claude looks for ideas here.
- **validation**: `test` scores a candidate here.
- **holdout**: locked. Nothing reads it except `holdout`, once per champion version.

A candidate is **promotable** only when all five checks pass:

1. **trades**: at least `min_trades` (20) on validation.
2. **beats**: validation average R minus a noise bar is still above the
   champion's validation average R. The bar is k standard errors, k =
   sqrt(2 ln(N + 1)), where N counts every experiment on this snapshot. The
   bar rises with each try, because more tries on the same data turn up more
   lucky winners.
3. **costs**: still positive on validation at twice the costs.
4. **windows**: positive in at least 3 of 4 time windows across development
   and validation.
5. **neighbors**: nudging each number one step either way keeps the median
   neighbor positive. A lone spike is overfit.

## The loop (Claude's part)

1. Run `diagnose`. Read the buckets: range width, gap, entry time, breakout
   volume, symbol. Buckets with fewer than about 10 trades are noise. Say so.
2. Write ONE hypothesis in a sentence, and ONE change that tests it, to
   `trader/lab/candidate.json`. One change per test, so the log shows what helped.
3. Run `test` with `--note` set to the hypothesis. Report every check exactly
   as printed, PASS and FAIL alike.
4. If it printed PROMOTABLE, tell the user and promote only after they say
   yes. If not, the champion stays. Do not tweak the same idea until it
   passes: that is curve fitting, and the noise bar is there to catch it.
5. Stop after about 10 experiments on one snapshot and tell the user where
   things stand. More ideas tested on the same 60 days means more lucky winners.
6. After a promotion, run `holdout` once and report it as printed. Never
   change a spec because of the holdout. If it fails, the edge is unproven.
7. The real test is the future. Let `forward` collect new sessions while the
   champion is paper traded through the normal chain, using the commands
   `show` prints.

## Example: the reel's data

TSLA alone, 59 sessions to 2026-10-16. Champion v1, the reel's rule: 20
trades on development, avg +0.020R. `diagnose` shows wide opening ranges
doing no better than the rest. The hypothesis "skip opening ranges wider than
2 %" scored +0.081R on development but -0.345R on validation. It failed
beats, costs, windows and neighbors, so the champion stayed. That is a normal
result. One stock over 12 validation days is far too few trades to tell an
edge from luck. Add symbols.

## Rules

- Every number comes from the tool. Never round a coin flip up into an edge,
  and never call a backtest a prediction.
- `diagnose` is the only place ideas come from. Never look at holdout numbers
  for ideas, never test against them, never re-run `init --force` to get a
  better split.
- One change per experiment. Log the hypothesis.
- Promote only on PROMOTABLE and the user's yes.
- The lab changes the rule, never the limits. Risk, budget, paper mode and
  the broker's yes stay exactly as they are in `trader/config.json`.

## What it never does

It never places or sizes an order, never edits `trader/config.json`, never
reads the holdout during a test, never promotes a failed experiment, and
never deletes the experiment log (`init --force` keeps the old one, dated).

## Safety

- Education and tooling, not financial advice. A PROMOTABLE is a statistics check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
