#!/usr/bin/env python3
"""Strategy Lab for trader-lab: a controlled self-improvement loop for the
opening-range breakout. Standard library only. Long side only. It never
places an order and never changes your risk limits.

    python3 lab.py fetch --symbols TSLA,NVDA,AMD,AAPL,META   # freeze 60 days of 5-min bars
    python3 lab.py init                                      # split the dates, champion v1 = the reel's rule
    python3 lab.py diagnose                                  # where the champion loses (development dates only)
    python3 lab.py test trader/lab/candidate.json --note "skip wide opening ranges"
    python3 lab.py promote 3                                 # only an experiment that passed every check
    python3 lab.py log                                       # every experiment, kept or not
    python3 lab.py show                                      # the champion and the commands it hands off
    python3 lab.py holdout                                   # the champion on the locked dates, once per version
    python3 lab.py forward                                   # the champion on sessions after the snapshot
    python3 lab.py check TSLA                                # today's session against the champion's filters

The loop (the reasoning step is Claude, reading diagnose):
  observe (diagnose) -> hypothesize ONE change -> test -> keep or revert -> log.

The data is frozen once (fetch), then split by date across every symbol:
  development  the first 60 % of the dates. diagnose only ever reads these.
  validation   the next 20 %. test scores a candidate here.
  holdout      the last 20 %. Locked: test refuses to look at it. holdout runs
               the champion on it once per champion version and records it.

A candidate is promotable only when every check passes:
  1. trades      at least min_trades (default 20) on the validation dates.
  2. beats       validation average R minus a noise bar beats the champion's
                 validation average R. The noise bar is k x the standard error
                 of the candidate's mean R, k = sqrt(2 ln(N + 1)), where N is
                 the number of experiments tested on this snapshot so far. Every
                 try raises the bar: the more ideas you test on the same data,
                 the more of them look good by luck.
  3. costs       still positive on validation at twice the costs.
  4. windows     positive average R in at least 3 of the 4 time windows of the
                 development + validation dates (a strategy that only works in
                 one stretch is a fluke).
  5. neighbors   nudging each number (one step up and down) keeps the median
                 average R of the neighbors above zero (a lone spike is overfit).

Trades: entry at the close of the first 5-minute bar after the opening range
that closes above the range high (inside the entry window), the same rule as
trader-entry. Stop and target as trader-exit (stop rounded down to the cent,
target = entry + R x risk). Bar by bar the stop is checked first; a bar that
opens through the stop fills at its open, not at the stop. Flat at flat_by.
Only the first breakout of a day counts: if it fails a filter, no trade.
Costs: slippage (basis points) on the entry and on stop and close exits (a
target is a limit and fills at its price), plus commission per share each way.
R = (exit fill - entry fill - commissions) / (entry - stop).

60 days of 5-minute bars is a small sample. Most candidates should be
rejected, and that is the tool working. Backtests are not predictions.
Data: https://query2.finance.yahoo.com/v8/finance/chart/<T>?range=60d&interval=5m
(unofficial and free, can lag). Times are New York (ET) wall-clock.
Exit codes: test 0 = promotable, 2 = keep the champion; 1 = an error.
"""

import argparse
import datetime
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars
SESSION_BARS = 78    # 9:30 to 16:00
OPEN_MIN = 9 * 60 + 30
EPOCH = datetime.datetime(1970, 1, 1)
LAB = os.path.join("trader", "lab")
SPLIT = (0.6, 0.8)   # development | validation | holdout, by date
WINDOWS = 4
MIN_WINDOWS = 3
MIN_DATES = 30
MIN_RISK = 0.0005    # risk under 0.05 % of the price is inside the spread (trader-exit's rule)
COSTS = {"slippage_bps": 3.0, "commission_per_share": 0.0}
BASE = {"name": "orb-15m-mid-1R", "or_minutes": 15, "stop_rule": "or_mid", "stop_pct": None, "target_r": 1.0,
        "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
        "min_or_pct": None, "max_or_pct": None, "min_gap_pct": None, "max_gap_pct": None,
        "min_breakout_vol": None, "above_prev_close": False}
STOP_RULES = ("or_mid", "or_low", "pct")
# one step per number, for the neighbors check
STEPS = {"or_minutes": 5, "target_r": 0.5, "stop_pct": 0.25, "min_or_pct": 0.2, "max_or_pct": 0.2,
         "min_gap_pct": 0.5, "max_gap_pct": 0.5, "min_breakout_vol": 0.25}
FLOORS = {"or_minutes": 5, "target_r": 0.5, "stop_pct": 0.25, "min_or_pct": 0.0, "max_or_pct": 0.1,
          "min_breakout_vol": 0.0}


class LabError(Exception):
    pass


# ---------------------------------------------------------------- time (copied from trader-timemachine)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise LabError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_clock(text, what):
    """'10:15 AM', '15:55' or '3:55' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise LabError("%s wants a New York time like 10:15 AM or 15:55, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise LabError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock12(minutes):
    h, m = divmod(minutes, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


# ---------------------------------------------------------------- data (as trader-timemachine)

def chart_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, urllib.parse.quote(sym.upper().replace(".", "-")))


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym):
    """One request for 60 days of 5-min bars: query2, one retry after a 429, then query1."""
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            try:
                return _get(chart_url(sym, host))
            except urllib.error.HTTPError as e:
                last = e.code
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise LabError("Yahoo has no chart for %s (HTTP 404): check the ticker" % sym.upper())
                raise LabError("Yahoo answered HTTP %d for %s" % (e.code, sym.upper()))
            except (urllib.error.URLError, OSError) as e:
                raise LabError("could not reach Yahoo: %s" % getattr(e, "reason", e))
    if last == 429:
        raise LabError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise LabError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, sym.upper()))


def read_text(path):
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise LabError("could not read %s: %s" % (path, e))


def offline_file(path, sym):
    """A saved Yahoo chart file, or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json.gz" % sym, "%s-60d5m.json" % sym):
            if os.path.exists(os.path.join(path, name)):
                return os.path.join(path, name)
        raise LabError("no %s-60d5m.json(.gz) in %s" % (sym, path))
    if not os.path.exists(path):
        raise LabError("no offline chart file %s" % path)
    return path


def chart_result(text, sym):
    try:
        data = json.loads(text)
    except ValueError:
        raise LabError("the chart for %s is not JSON" % sym)
    chart = data.get("chart") if isinstance(data, dict) else None
    res = chart.get("result") if isinstance(chart, dict) else None
    if not res:
        raise LabError("Yahoo has no data for %s" % sym)
    return res[0]


def parse_bars(result, now_utc):
    """Regular-session 5-min bars that have closed by now (or by the data's own
    regularMarketTime), oldest first, as {date, slot, o, h, l, c, v}."""
    meta = result.get("meta") or {}
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    bars = {}
    for i, ts in enumerate(result.get("timestamp") or []):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c) or int(ts) % INTERVAL:
            continue
        start = utc_of(ts)
        local = to_eastern(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5 or start + datetime.timedelta(seconds=INTERVAL) > as_of:
            continue
        bars[int(ts)] = {"date": local.date().isoformat(), "slot": mins // 5,
                         "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return [bars[t] for t in sorted(bars)]


def sessions_of(bars):
    """[(date, 78 bars, previous session's last close or None)] for full sessions only."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    out, prev = [], None
    for d in sorted(days):
        s = days[d]
        if len(s) == SESSION_BARS:
            out.append((d, [s[i] for i in range(SESSION_BARS)], prev))
        prev = s[max(s)]["c"]
    return out


# ---------------------------------------------------------------- the spec

def normalize_spec(raw):
    if not isinstance(raw, dict):
        raise LabError("a spec is a JSON object")
    unknown = sorted(set(raw) - set(BASE) - {"note"})
    if unknown:
        raise LabError("unknown spec field(s): %s. The fields are: %s" % (", ".join(unknown), ", ".join(BASE)))
    s = dict(BASE)
    s.update({k: v for k, v in raw.items() if k != "note"})
    if not isinstance(s["or_minutes"], int) or s["or_minutes"] % 5 or not 5 <= s["or_minutes"] <= 60:
        raise LabError("or_minutes is 5 to 60, in steps of 5")
    if s["stop_rule"] not in STOP_RULES:
        raise LabError("stop_rule is one of %s" % ", ".join(STOP_RULES))
    if s["stop_rule"] == "pct" and not (isinstance(s["stop_pct"], (int, float)) and 0 < s["stop_pct"] < 20):
        raise LabError("stop_rule pct needs stop_pct between 0 and 20")
    if s["stop_rule"] != "pct":
        s["stop_pct"] = None
    if not isinstance(s["target_r"], (int, float)) or not 0.25 <= s["target_r"] <= 10:
        raise LabError("target_r is 0.25 to 10")
    for k in ("min_or_pct", "max_or_pct", "min_gap_pct", "max_gap_pct", "min_breakout_vol"):
        if s[k] is not None and not isinstance(s[k], (int, float)):
            raise LabError("%s is a number or null" % k)
    if s["min_or_pct"] is not None and s["max_or_pct"] is not None and s["min_or_pct"] >= s["max_or_pct"]:
        raise LabError("min_or_pct must be under max_or_pct")
    if s["min_gap_pct"] is not None and s["max_gap_pct"] is not None and s["min_gap_pct"] >= s["max_gap_pct"]:
        raise LabError("min_gap_pct must be under max_gap_pct")
    if not isinstance(s["above_prev_close"], bool):
        raise LabError("above_prev_close is true or false")
    first, last, flat = (parse_clock(s[k], k) for k in ("no_entries_before", "no_entries_after", "flat_by"))
    if not OPEN_MIN + s["or_minutes"] <= last < flat <= OPEN_MIN + SESSION_BARS * 5 or first > last:
        raise LabError("the entry window must sit after the opening range and before flat_by")
    if (flat - OPEN_MIN) % 5:
        raise LabError("flat_by must be on the 5-minute grid")
    s["name"] = str(s.get("name") or "unnamed")
    return s


def spec_hash(spec):
    body = {k: v for k, v in spec.items() if k != "name"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def describe(spec, base=None):
    """The fields that differ from base (default: the reel's rule), as k=v."""
    base = base or BASE
    diff = ["%s=%s" % (k, json.dumps(spec[k])) for k in BASE if k != "name" and spec[k] != base.get(k)]
    return ", ".join(diff) or "the reel's rule"


# ---------------------------------------------------------------- one day, one trade

def cents_down(x):
    return math.floor(round(x * 100, 4)) / 100.0


def day_trade(spec, D, prev, costs):
    """The spec on one full session. -> (trade or None, reason when None)."""
    n = spec["or_minutes"] // 5
    oh = max(b["h"] for b in D[:n])
    ol = min(b["l"] for b in D[:n])
    or_pct = (oh - ol) / ol * 100
    gap_pct = (D[0]["o"] / prev - 1) * 100 if prev else None
    if spec["min_or_pct"] is not None and or_pct < spec["min_or_pct"]:
        return None, "range too narrow"
    if spec["max_or_pct"] is not None and or_pct > spec["max_or_pct"]:
        return None, "range too wide"
    if spec["min_gap_pct"] is not None or spec["max_gap_pct"] is not None or spec["above_prev_close"]:
        if gap_pct is None:
            return None, "no previous close"
    if spec["min_gap_pct"] is not None and gap_pct < spec["min_gap_pct"]:
        return None, "gap too small"
    if spec["max_gap_pct"] is not None and gap_pct > spec["max_gap_pct"]:
        return None, "gap too big"
    # bar k closes at OPEN_MIN + 5 (k + 1)
    first = max(n, (parse_clock(spec["no_entries_before"], "") - OPEN_MIN) // 5 - 1)
    last_entry = (parse_clock(spec["no_entries_after"], "") - OPEN_MIN) // 5 - 1
    flat = (parse_clock(spec["flat_by"], "") - OPEN_MIN) // 5 - 1
    k = next((i for i in range(first, min(last_entry, flat - 1) + 1) if D[i]["c"] > oh), None)
    if k is None:
        return None, "no breakout"
    or_vol = statistics.mean(b["v"] for b in D[:n])
    vol_ratio = D[k]["v"] / or_vol if or_vol else None
    if spec["min_breakout_vol"] is not None and (vol_ratio is None or vol_ratio < spec["min_breakout_vol"]):
        return None, "breakout volume too light"
    e = round(D[k]["c"], 2)
    if spec["above_prev_close"] and e <= prev:
        return None, "below the previous close"
    if spec["stop_rule"] == "or_mid":
        s = cents_down((oh + ol) / 2)
    elif spec["stop_rule"] == "or_low":
        s = cents_down(ol)
    else:
        s = cents_down(e * (1 - spec["stop_pct"] / 100.0))
    risk = e - s
    if risk <= 0 or risk / e < MIN_RISK:
        return None, "stop too tight"
    t = round(e + spec["target_r"] * risk, 2)
    out, x = "close", D[flat]["c"]
    for b in D[k + 1:flat + 1]:
        if b["o"] <= s:
            out, x = "stop", b["o"]     # gapped through the stop: fills at the open
            break
        if b["l"] <= s:
            out, x = "stop", s
            break
        if b["h"] >= t:
            out, x = "target", t
            break
    slip = costs["slippage_bps"] / 1e4
    fill_in = e * (1 + slip)
    fill_out = x if out == "target" else x * (1 - slip)
    net = (fill_out - fill_in - 2 * costs["commission_per_share"]) / risk
    return {"entry_time": clock12(OPEN_MIN + 5 * (k + 1)), "entry": e, "stop": s, "target": t,
            "exit": round(x, 2), "outcome": out, "R_gross": round((x - e) / risk, 3), "R": round(net, 3),
            "or_pct": round(or_pct, 3), "gap_pct": None if gap_pct is None else round(gap_pct, 3),
            "vol_ratio": None if vol_ratio is None else round(vol_ratio, 2), "slot": k}, None


def backtest(spec, data, dates, costs):
    """-> (trades in date order, skip reasons)."""
    want = set(dates)
    trades, skips = [], {}
    for sym in sorted(data):
        for d, D, prev in data[sym]:
            if d not in want:
                continue
            t, why = day_trade(spec, D, prev, costs)
            if t is None:
                skips[why] = skips.get(why, 0) + 1
            else:
                t.update(symbol=sym, date=d)
                trades.append(t)
    trades.sort(key=lambda t: (t["date"], t["slot"], t["symbol"]))
    return trades, skips


def metrics(trades):
    rs = [t["R"] for t in trades]
    n = len(rs)
    if not n:
        return {"trades": 0, "avg_R": None, "se": None, "t": None, "win_pct": None, "total_R": 0.0,
                "max_dd_R": 0.0, "profit_factor": None, "target": 0, "stop": 0, "close": 0}
    avg = statistics.mean(rs)
    se = statistics.stdev(rs) / math.sqrt(n) if n > 1 else None
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    gains, losses = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    return {"trades": n, "avg_R": round(avg, 4), "se": None if se is None else round(se, 4),
            "t": round(avg / se, 2) if se else None, "win_pct": round(100.0 * sum(1 for r in rs if r > 0) / n, 1),
            "total_R": round(sum(rs), 2), "max_dd_R": round(dd, 2),
            "profit_factor": round(gains / losses, 2) if losses else None,
            "target": sum(1 for t in trades if t["outcome"] == "target"),
            "stop": sum(1 for t in trades if t["outcome"] == "stop"),
            "close": sum(1 for t in trades if t["outcome"] == "close")}


# ---------------------------------------------------------------- the lab's state

def lab_path(*parts):
    return os.path.join(LAB, *parts)


def _write(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_lab():
    path = lab_path("lab.json")
    if not os.path.exists(path):
        raise LabError("no %s yet: run lab.py fetch, then lab.py init" % path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_data(symbols, now_utc=None):
    now_utc = now_utc or parse_now(None)
    data = {}
    for sym in symbols:
        path = offline_file(lab_path("data"), sym)
        data[sym] = sessions_of(parse_bars(chart_result(read_text(path), sym), now_utc))
    return data


def experiments():
    path = lab_path("experiments.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def champion(lab):
    return lab["champions"][-1]


def split_dates(data):
    dates = sorted({d for sessions in data.values() for d, _, _ in sessions})
    if len(dates) < MIN_DATES:
        raise LabError("only %d full sessions in the data; the lab needs at least %d" % (len(dates), MIN_DATES))
    i, j = int(len(dates) * SPLIT[0]), int(len(dates) * SPLIT[1])
    return {"development": dates[:i], "validation": dates[i:j], "holdout": dates[j:]}


def dates_of(lab, part):
    a, b = lab["split"][part]
    return [d for d in lab["dates"] if a <= d <= b]


def load_spec(path):
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as e:
        raise LabError("could not read %s: %s" % (path, e))
    except ValueError as e:
        raise LabError("%s is not valid JSON: %s" % (path, e))
    return normalize_spec(raw), raw.get("note")


# ---------------------------------------------------------------- the checks

def neighbors(spec):
    out = []
    for k, step in STEPS.items():
        v = spec.get(k)
        if v is None:
            continue
        for sign in (-1, 1):
            nv = v + sign * step
            if k in FLOORS and nv < FLOORS[k]:
                continue
            if k == "or_minutes" and nv > 60:
                continue
            raw = dict(spec, **{k: nv})
            try:
                out.append((k, nv, normalize_spec(raw)))
            except LabError:
                continue
    return out


def noise_k(n_tested):
    return math.sqrt(2 * math.log(n_tested + 1))


def evaluate(lab, data, spec, n_tested):
    costs = lab["costs"]
    stress = {k: v * 2 for k, v in costs.items()}
    val = dates_of(lab, "validation")
    dv = dates_of(lab, "development") + val
    champ = champion(lab)["spec"]
    m_dev = metrics(backtest(spec, data, dates_of(lab, "development"), costs)[0])
    m_val = metrics(backtest(spec, data, val, costs)[0])
    m_champ = metrics(backtest(champ, data, val, costs)[0])
    m_stress = metrics(backtest(spec, data, val, stress)[0])
    size = int(math.ceil(len(dv) / float(WINDOWS)))
    wins = []
    for w in range(WINDOWS):
        part = dv[w * size:(w + 1) * size]
        if part:
            m = metrics(backtest(spec, data, part, costs)[0])
            wins.append({"from": part[0], "to": part[-1], "trades": m["trades"], "avg_R": m["avg_R"]})
    neigh = []
    for k, v, ns in neighbors(spec):
        m = metrics(backtest(ns, data, dv, costs)[0])
        neigh.append({"field": k, "value": v, "trades": m["trades"], "avg_R": m["avg_R"]})
    k = noise_k(n_tested)
    champ_avg = m_champ["avg_R"] if m_champ["avg_R"] is not None else 0.0
    bar = (m_val["se"] or 0.0) * k
    checks = []
    checks.append(("trades", m_val["trades"] >= lab["min_trades"],
                   "%d validation trades, need %d" % (m_val["trades"], lab["min_trades"])))
    if m_val["avg_R"] is None:
        checks.append(("beats", False, "no validation trades"))
    else:
        checks.append(("beats", m_val["avg_R"] - bar > champ_avg,
                       "%+.3fR - noise bar %.3f (k %.2f after %d tests) = %+.3fR vs champion %+.3fR" % (
                           m_val["avg_R"], bar, k, n_tested, m_val["avg_R"] - bar, champ_avg)))
    checks.append(("costs", (m_stress["avg_R"] or 0) > 0,
                   "%s at twice the costs" % ("no trades" if m_stress["avg_R"] is None
                                              else "%+.3fR" % m_stress["avg_R"])))
    good = sum(1 for w in wins if (w["avg_R"] or 0) > 0)
    checks.append(("windows", good >= MIN_WINDOWS, "%d of %d windows positive, need %d" % (good, len(wins), MIN_WINDOWS)))
    if neigh:
        med = statistics.median(x["avg_R"] or 0 for x in neigh)
        checks.append(("neighbors", med > 0, "median neighbor %+.3fR over %d nudges" % (med, len(neigh))))
    else:
        checks.append(("neighbors", False, "no numbers to nudge"))
    return {"development": m_dev, "validation": m_val, "champion_validation": m_champ, "stress": m_stress,
            "windows": wins, "neighbors": neigh, "noise_k": round(k, 3),
            "checks": [{"name": a, "pass": b, "detail": c} for a, b, c in checks],
            "promotable": all(b for _, b, _ in checks)}


# ---------------------------------------------------------------- printing

def fmt_m(m):
    if not m["trades"]:
        return "0 trades"
    return "%d trades, avg %+.3fR (se %s), win %.1f %%, total %+.2fR, max drawdown %.2fR, PF %s" % (
        m["trades"], m["avg_R"], "%.3f" % m["se"] if m["se"] is not None else "n/a", m["win_pct"], m["total_R"],
        m["max_dd_R"], m["profit_factor"] if m["profit_factor"] is not None else "n/a")


def report_test(exp):
    ev = exp["result"]
    out = ["Experiment #%d: %s" % (exp["id"], exp["spec"]["name"]),
           "  change: %s (vs champion v%d)" % (exp["change"], exp["parent"])]
    if exp.get("note"):
        out.append("  hypothesis: %s" % exp["note"])
    out += ["  development  %s" % fmt_m(ev["development"]),
            "  validation   %s" % fmt_m(ev["validation"]),
            "  champion     %s (validation)" % fmt_m(ev["champion_validation"]),
            "  2x costs     %s (validation)" % fmt_m(ev["stress"]),
            "  windows      " + "  ".join("%s..%s %s" % (w["from"][5:], w["to"][5:],
                                                          "n/a" if w["avg_R"] is None else "%+.2fR" % w["avg_R"])
                                          for w in ev["windows"])]
    for c in ev["checks"]:
        out.append("  [%s] %-9s %s" % ("PASS" if c["pass"] else "FAIL", c["name"], c["detail"]))
    out.append("PROMOTABLE: lab.py promote %d" % exp["id"] if ev["promotable"]
               else "KEEP CHAMPION v%d. Logged; the idea did not survive every check." % exp["parent"])
    return "\n".join(out)


def bucket_table(trades, title, key, edges, labels):
    rows = {}
    for t in trades:
        v = key(t)
        if v is None:
            lab_ = "n/a"
        else:
            lab_ = labels[sum(1 for e in edges if v >= e)]
        rows.setdefault(lab_, []).append(t["R"])
    out = ["  %s" % title]
    for lab_ in labels + ["n/a"]:
        if lab_ in rows:
            rs = rows[lab_]
            out.append("    %-22s %3d trades  avg %+.3fR  win %5.1f %%" % (
                lab_, len(rs), statistics.mean(rs), 100.0 * sum(1 for r in rs if r > 0) / len(rs)))
    return out


def terciles(vals):
    vals = sorted(v for v in vals if v is not None)
    if len(vals) < 3:
        return []
    return [vals[len(vals) // 3], vals[2 * len(vals) // 3]]


# ---------------------------------------------------------------- commands

def cmd_fetch(a):
    syms = [s.strip().upper() for s in (a.symbols or "").split(",") if s.strip()]
    if not syms:
        raise LabError("give --symbols, e.g. --symbols TSLA,NVDA,AMD,AAPL,META")
    os.makedirs(lab_path("data"), exist_ok=True)
    for n, sym in enumerate(syms):
        if a.offline:
            src = offline_file(a.offline, sym)
            text = read_text(src)
        else:
            if n:
                _sleep(PAUSE)
            text = fetch_chart(sym)
        chart_result(text, sym)
        with gzip.open(lab_path("data", "%s-60d5m.json.gz" % sym), "wt", encoding="utf-8") as fh:
            fh.write(text)
        print("saved %s" % lab_path("data", "%s-60d5m.json.gz" % sym))
    print("Frozen. Run lab.py init to split the dates and start champion v1.")
    return 0


def cmd_init(a):
    path = lab_path("lab.json")
    if os.path.exists(path) and not a.force:
        raise LabError("%s exists. --force starts a new snapshot and a new experiment count "
                       "(the old log is kept as experiments-<date>.jsonl)" % path)
    names = sorted(f.split("-60d5m")[0] for f in os.listdir(lab_path("data")) if "-60d5m.json" in f) \
        if os.path.isdir(lab_path("data")) else []
    if not names:
        raise LabError("no data in %s: run lab.py fetch --symbols ... first" % lab_path("data"))
    now = parse_now(a.now)
    data = load_data(names, now)
    parts = split_dates(data)
    spec = load_spec(a.spec)[0] if a.spec else normalize_spec(dict(BASE))
    if os.path.exists(lab_path("experiments.jsonl")):
        shutil.move(lab_path("experiments.jsonl"),
                    lab_path("experiments-%s.jsonl" % now.strftime("%Y%m%d-%H%M%S")))
    lab = {"created_by": "trader-lab", "created": now.isoformat() + "Z", "symbols": names,
           "dates": sorted(d for p in parts.values() for d in p),
           "split": {k: [v[0], v[-1]] for k, v in parts.items()},
           "costs": dict(COSTS, **({"slippage_bps": a.slippage_bps} if a.slippage_bps is not None else {})),
           "min_trades": a.min_trades,
           "champions": [{"version": 1, "spec": spec, "hash": spec_hash(spec), "from": "baseline",
                          "at": now.isoformat() + "Z", "holdout": None}]}
    _write(path, lab)
    print("Lab ready: %d symbols, %d sessions." % (len(names), len(lab["dates"])))
    for k in ("development", "validation", "holdout"):
        print("  %-12s %s to %s (%d dates)" % (k, parts[k][0], parts[k][-1], len(parts[k])))
    print("Champion v1: %s (%s). The holdout dates are locked until lab.py holdout." % (spec["name"], describe(spec)))
    return 0


def cmd_diagnose(a):
    lab = load_lab()
    data = load_data(lab["symbols"])
    spec = load_spec(a.spec)[0] if a.spec else champion(lab)["spec"]
    trades, skips = backtest(spec, data, dates_of(lab, "development"), lab["costs"])
    if a.json:
        print(json.dumps({"spec": spec, "metrics": metrics(trades), "skips": skips, "trades": trades}, indent=2))
        return 0
    out = ["Diagnose %s on the development dates only (%s to %s)." % (
        spec["name"], lab["split"]["development"][0], lab["split"]["development"][1]),
        "  %s" % fmt_m(metrics(trades)),
        "  no trade: " + (", ".join("%s %d" % kv for kv in sorted(skips.items())) or "none")]
    if trades:
        e = terciles([t["or_pct"] for t in trades])
        if e:
            out += bucket_table(trades, "opening range width (% of price)", lambda t: t["or_pct"], e,
                                ["narrow (< %.2f %%)" % e[0], "middle", "wide (>= %.2f %%)" % e[1]])
        out += bucket_table(trades, "gap from the previous close", lambda t: t["gap_pct"], [-1.0, 1.0],
                            ["gap down (< -1 %)", "flat (-1 to 1 %)", "gap up (> 1 %)"])
        out += bucket_table(trades, "entry time", lambda t: t["slot"], [12, 30],
                            ["10:30 AM or earlier", "10:35 AM to 12:00 PM", "12:05 PM or later"])
        v = terciles([t["vol_ratio"] for t in trades])
        if v:
            out += bucket_table(trades, "breakout bar volume / opening-range average", lambda t: t["vol_ratio"], v,
                                ["light (< %.2fx)" % v[0], "middle", "heavy (>= %.2fx)" % v[1]])
        out += bucket_table(trades, "symbol", lambda t: sorted(data).index(t["symbol"]),
                            list(range(1, len(data))), sorted(data))
        out.append("  outcomes: %d target, %d stop, %d flat at %s" % (
            sum(1 for t in trades if t["outcome"] == "target"), sum(1 for t in trades if t["outcome"] == "stop"),
            sum(1 for t in trades if t["outcome"] == "close"), spec["flat_by"]))
    out.append("Small buckets are noise. Propose ONE change, write it as a spec, then lab.py test it.")
    print("\n".join(out))
    return 0


def cmd_test(a):
    lab = load_lab()
    spec, note = load_spec(a.spec)
    note = a.note or note
    log = experiments()
    h = spec_hash(spec)
    seen = next((x for x in log if x["hash"] == h), None)
    if seen:
        raise LabError("this exact spec was already tested as experiment #%d (%s). Re-testing it adds no "
                       "information; change something." % (seen["id"], "promotable" if seen["result"]["promotable"]
                                                           else "kept the champion"))
    if h == champion(lab)["hash"]:
        raise LabError("that is the champion itself")
    data = load_data(lab["symbols"])
    n = len(log) + 1
    ev = evaluate(lab, data, spec, n)
    exp = {"id": n, "at": parse_now(None).isoformat() + "Z", "spec": spec, "hash": h, "note": note,
           "parent": champion(lab)["version"], "change": describe(spec, champion(lab)["spec"]), "result": ev}
    os.makedirs(LAB, exist_ok=True)
    with open(lab_path("experiments.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(exp) + "\n")
    print(json.dumps(exp, indent=2) if a.json else report_test(exp))
    return 0 if ev["promotable"] else 2


def cmd_promote(a):
    lab = load_lab()
    exp = next((x for x in experiments() if x["id"] == a.id), None)
    if exp is None:
        raise LabError("no experiment #%d" % a.id)
    if not exp["result"]["promotable"]:
        fails = ", ".join(c["name"] for c in exp["result"]["checks"] if not c["pass"])
        raise LabError("experiment #%d failed: %s. Only an experiment that passed every check is promoted." % (a.id, fails))
    cur = champion(lab)
    if exp["parent"] != cur["version"]:
        raise LabError("experiment #%d was tested against champion v%d; the champion is now v%d. Test it again."
                       % (a.id, exp["parent"], cur["version"]))
    lab["champions"].append({"version": cur["version"] + 1, "spec": exp["spec"], "hash": exp["hash"],
                             "from": "experiment #%d" % a.id, "at": parse_now(None).isoformat() + "Z",
                             "holdout": None})
    _write(lab_path("lab.json"), lab)
    print("Champion v%d: %s (%s). Run lab.py holdout once, then paper trade it." % (
        cur["version"] + 1, exp["spec"]["name"], exp["change"]))
    return 0


def cmd_holdout(a):
    lab = load_lab()
    cur = champion(lab)
    if cur["holdout"] is not None:
        print("Champion v%d was already run on the holdout (%s): %s" % (cur["version"], cur["holdout"]["at"],
                                                                       fmt_m(cur["holdout"]["metrics"])))
        print("It runs once per version. Do not tune against it; lab.py forward is the next honest test.")
        return 0
    data = load_data(lab["symbols"])
    m = metrics(backtest(cur["spec"], data, dates_of(lab, "holdout"), lab["costs"])[0])
    cur["holdout"] = {"at": parse_now(None).isoformat() + "Z", "metrics": m}
    _write(lab_path("lab.json"), lab)
    print("Holdout %s to %s, champion v%d (%s), once:" % (lab["split"]["holdout"][0], lab["split"]["holdout"][1],
                                                         cur["version"], cur["spec"]["name"]))
    print("  %s" % fmt_m(m))
    if not m["trades"] or m["avg_R"] <= 0:
        print("Not positive on dates it never saw. Treat the edge as unproven; do not retune against this.")
    else:
        print("Positive on dates it never saw. Still a small sample: paper trade it before anything else.")
    return 0


def cmd_forward(a):
    lab = load_lab()
    cur = champion(lab)
    now = parse_now(a.now)
    data = {}
    for n, sym in enumerate(lab["symbols"]):
        if a.offline:
            text = read_text(offline_file(a.offline, sym))
        else:
            if n:
                _sleep(PAUSE)
            text = fetch_chart(sym)
        data[sym] = [s for s in sessions_of(parse_bars(chart_result(text, sym), now)) if s[0] > lab["dates"][-1]]
    dates = sorted({d for ss in data.values() for d, _, _ in ss})
    if not dates:
        print("No full session after %s yet. Run it again after the next close." % lab["dates"][-1])
        return 0
    trades = backtest(cur["spec"], data, dates, lab["costs"])[0]
    print("Forward test, champion v%d, %s to %s (%d sessions after the snapshot):" % (
        cur["version"], dates[0], dates[-1], len(dates)))
    print("  %s" % fmt_m(metrics(trades)))
    for t in trades:
        print("  %s %-5s %s entry %.2f stop %.2f target %.2f -> %s %+.2fR" % (
            t["date"], t["symbol"], t["entry_time"], t["entry"], t["stop"], t["target"], t["outcome"], t["R"]))
    return 0


def cmd_log(a):
    log = experiments()
    if a.json:
        print(json.dumps(log, indent=2))
        return 0
    if not log:
        print("No experiments yet.")
        return 0
    for x in log:
        v = x["result"]["validation"]
        fails = [c["name"] for c in x["result"]["checks"] if not c["pass"]]
        print("#%-3d v%d  %-28s val %s  %s" % (
            x["id"], x["parent"], x["spec"]["name"][:28],
            "n/a    " if v["avg_R"] is None else "%+.3fR" % v["avg_R"],
            "PROMOTABLE" if not fails else "kept champion (" + ", ".join(fails) + ")"))
        print("      %s" % x["change"])
    return 0


def handoff(spec):
    stop = {"or_mid": "or-mid", "or_low": "or-low"}.get(spec["stop_rule"]) or "pct:%g" % spec["stop_pct"]
    return ["python3 trader-entry/entry.py check SYMBOL --setup orb --or-minutes %d" % spec["or_minutes"],
            "python3 trader-lab/lab.py check SYMBOL",
            "python3 trader-exit/exit.py plan --or-minutes %d --stop %s --target %gR --write" % (
                spec["or_minutes"], stop, spec["target_r"]),
            "python3 trader-timemachine/timemachine.py run --until %s --write" % spec["flat_by"],
            "then /trader-budget, /trader-risk, /trader-broker as usual (paper first)"]


def cmd_show(a):
    lab = load_lab()
    cur = champion(lab)
    if a.json:
        print(json.dumps(cur, indent=2))
        return 0
    print("Champion v%d: %s, from %s." % (cur["version"], cur["spec"]["name"], cur["from"]))
    print("  rule: %s" % describe(cur["spec"]))
    print("  entry window %s to %s, flat by %s" % (cur["spec"]["no_entries_before"], cur["spec"]["no_entries_after"],
                                                  cur["spec"]["flat_by"]))
    print("  holdout: %s" % ("not run yet" if cur["holdout"] is None else fmt_m(cur["holdout"]["metrics"])))
    print("Hand-off to the day-trade chain:")
    for line in handoff(cur["spec"]):
        print("  " + line)
    return 0


def cmd_check(a):
    lab = load_lab()
    spec = champion(lab)["spec"]
    sym = a.symbol.upper()
    now = parse_now(a.now)
    text = read_text(offline_file(a.offline, sym)) if a.offline else fetch_chart(sym)
    bars = parse_bars(chart_result(text, sym), now)
    if not bars:
        raise LabError("no closed regular-session bars for %s" % sym)
    day = bars[-1]["date"]
    D = {b["slot"]: b for b in bars if b["date"] == day}
    prev = next((b["c"] for b in reversed(bars) if b["date"] < day), None)
    n = spec["or_minutes"] // 5
    if any(i not in D for i in range(n)):
        print("%s %s: the %d-minute opening range is not complete yet." % (sym, day, spec["or_minutes"]))
        return 1
    oh, ol = max(D[i]["h"] for i in range(n)), min(D[i]["l"] for i in range(n))
    or_pct = (oh - ol) / ol * 100
    gap = (D[0]["o"] / prev - 1) * 100 if prev else None
    out, ok = ["%s %s against champion v%d (%s):" % (sym, day, champion(lab)["version"], spec["name"]),
               "  opening range %.2f to %.2f, %.2f %% wide; gap %s" % (
                   ol, oh, or_pct, "n/a" if gap is None else "%+.2f %%" % gap)], True

    def line(name, passed, detail):
        out.append("  [%s] %s: %s" % ("PASS" if passed else "FAIL", name, detail))
        return passed

    if spec["min_or_pct"] is not None:
        ok &= line("range width", or_pct >= spec["min_or_pct"], "needs >= %.2f %%" % spec["min_or_pct"])
    if spec["max_or_pct"] is not None:
        ok &= line("range width", or_pct <= spec["max_or_pct"], "needs <= %.2f %%" % spec["max_or_pct"])
    if spec["min_gap_pct"] is not None:
        ok &= line("gap", gap is not None and gap >= spec["min_gap_pct"], "needs >= %+.2f %%" % spec["min_gap_pct"])
    if spec["max_gap_pct"] is not None:
        ok &= line("gap", gap is not None and gap <= spec["max_gap_pct"], "needs <= %+.2f %%" % spec["max_gap_pct"])
    trig = next((D[i] for i in sorted(D) if i >= n and D[i]["c"] > oh), None)
    if trig is None:
        out.append("  no close above the range yet; trader-entry watches for it")
    else:
        or_vol = statistics.mean(D[i]["v"] for i in range(n))
        ratio = trig["v"] / or_vol if or_vol else None
        if spec["min_breakout_vol"] is not None:
            ok &= line("breakout volume", ratio is not None and ratio >= spec["min_breakout_vol"],
                       "%s, needs >= %.2fx" % ("n/a" if ratio is None else "%.2fx" % ratio, spec["min_breakout_vol"]))
        if spec["above_prev_close"]:
            ok &= line("above previous close", prev is not None and trig["c"] > prev,
                       "close %.2f vs %s" % (trig["c"], "n/a" if prev is None else "%.2f" % prev))
    out.append("Filters pass. Go on with /trader-exit." if ok else
               "The champion skips this day. No trade on this setup.")
    print("\n".join(out))
    return 0 if ok else 2


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lab.py", description="Strategy Lab: a controlled self-improvement loop.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("fetch")
    p.add_argument("--symbols")
    p.add_argument("--offline", metavar="PATH", help="copy saved Yahoo chart JSON instead of fetching")
    p = sub.add_parser("init")
    p.add_argument("--spec", help="start from this spec instead of the reel's rule")
    p.add_argument("--min-trades", type=int, default=20)
    p.add_argument("--slippage-bps", type=float)
    p.add_argument("--force", action="store_true")
    p.add_argument("--now")
    p = sub.add_parser("diagnose")
    p.add_argument("--spec")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("test")
    p.add_argument("spec")
    p.add_argument("--note", help="the hypothesis, in one line")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("promote")
    p.add_argument("id", type=int)
    sub.add_parser("holdout")
    p = sub.add_parser("forward")
    p.add_argument("--offline", metavar="PATH")
    p.add_argument("--now")
    p = sub.add_parser("log")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("symbol")
    p.add_argument("--offline", metavar="PATH")
    p.add_argument("--now")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return globals()["cmd_" + a.cmd](a)
    except LabError as e:
        print("lab: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
