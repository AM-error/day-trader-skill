import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

lab = load("trader-lab", "lab")

TM_DIR = os.path.join(FIX, "trader-timemachine")   # TSLA-60d5m.json.gz, fetched 2026-10-19 10:49 ET
NOW = lab.parse_now("2026-10-19 16:30")


def tsla():
    text = lab.read_text(os.path.join(TM_DIR, "TSLA-60d5m.json.gz"))
    return {"TSLA": lab.sessions_of(lab.parse_bars(lab.chart_result(text, "TSLA"), NOW))}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = lab.main(argv)
    return code, out.getvalue(), err.getvalue()


def bar(o, h, l, c, v=1000):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


class Backtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tsla()
        cls.dates = sorted(d for d, _, _ in cls.data["TSLA"])

    def test_full_sessions_only(self):
        self.assertEqual(len(self.dates), 59)
        self.assertNotIn("2026-10-19", self.dates)   # today is still open in the fixture

    def test_matches_the_time_machine_history(self):
        # the reel's rule with the time machine's window: enter up to the last bar, out at 4:00, no costs
        spec = lab.normalize_spec({"no_entries_after": "15:55", "flat_by": "16:00"})
        trades, _ = lab.backtest(spec, self.data, self.dates, {"slippage_bps": 0, "commission_per_share": 0})
        m = lab.metrics(trades)
        self.assertEqual((m["trades"], m["target"], m["stop"], m["close"]), (33, 13, 15, 5))

    def test_costs_only_lower_R(self):
        spec = lab.normalize_spec({})
        free = lab.backtest(spec, self.data, self.dates, {"slippage_bps": 0, "commission_per_share": 0})[0]
        paid = lab.backtest(spec, self.data, self.dates, {"slippage_bps": 5, "commission_per_share": 0.01})[0]
        self.assertEqual(len(free), len(paid))
        for a, b in zip(free, paid):
            self.assertLess(b["R"], a["R"] + 1e-9)

    def test_a_filter_only_removes_trades(self):
        base = lab.backtest(lab.normalize_spec({}), self.data, self.dates, lab.COSTS)[0]
        narrow = lab.backtest(lab.normalize_spec({"max_or_pct": 1.5}), self.data, self.dates, lab.COSTS)[0]
        self.assertLess(len(narrow), len(base))
        keys = {(t["date"], t["entry"]) for t in base}
        self.assertTrue(all((t["date"], t["entry"]) in keys for t in narrow))
        self.assertTrue(all(t["or_pct"] <= 1.5 for t in narrow))

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        D = [bar(100, 101, 99, 100)] * 3 + [bar(100, 102, 100, 101.5)] + [bar(95, 96, 94, 95)] * 74
        spec = lab.normalize_spec({})
        t, _ = lab.day_trade(spec, D, 100, {"slippage_bps": 0, "commission_per_share": 0})
        self.assertEqual((t["entry"], t["stop"], t["outcome"], t["exit"]), (101.5, 100.0, "stop", 95))
        self.assertLess(t["R"], -1)

    def test_stop_first_when_a_bar_touches_both(self):
        D = [bar(100, 101, 99, 100)] * 3 + [bar(100, 102, 100, 101.5)] + [bar(101.5, 110, 90, 101)] * 74
        t, _ = lab.day_trade(lab.normalize_spec({}), D, 100, {"slippage_bps": 0, "commission_per_share": 0})
        self.assertEqual(t["outcome"], "stop")

    def test_bad_specs_are_refused(self):
        for raw in ({"or_minutes": 7}, {"stop_rule": "atr"}, {"stop_rule": "pct"}, {"target_r": 0},
                    {"min_or_pct": 2, "max_or_pct": 1}, {"no_entries_after": "09:40"}, {"lever": 2},
                    {"above_prev_close": "yes"}):
            with self.assertRaises(lab.LabError, msg=raw):
                lab.normalize_spec(raw)

    def test_hash_ignores_the_name(self):
        a = lab.normalize_spec({"name": "a", "target_r": 2})
        b = lab.normalize_spec({"name": "b", "target_r": 2})
        self.assertEqual(lab.spec_hash(a), lab.spec_hash(b))
        self.assertNotEqual(lab.spec_hash(a), lab.spec_hash(lab.normalize_spec({})))

    def test_the_noise_bar_rises_with_every_test(self):
        ks = [lab.noise_k(n) for n in range(1, 30)]
        self.assertEqual(ks, sorted(ks))
        self.assertGreater(ks[0], 1)


class Loop(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.assertEqual(run(["fetch", "--symbols", "TSLA", "--offline", TM_DIR])[0], 0)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp)

    def spec(self, name, **kw):
        path = os.path.join(self.tmp, name + ".json")
        with open(path, "w") as fh:
            json.dump(dict(kw, name=name), fh)
        return path

    def test_init_splits_by_date(self):
        code, out, _ = run(["init", "--now", "2026-10-19 16:30"])
        self.assertEqual(code, 0, out)
        doc = lab.load_lab()
        self.assertEqual(doc["split"], {"development": ["2026-07-27", "2026-09-14"],
                                        "validation": ["2026-09-15", "2026-09-30"],
                                        "holdout": ["2026-10-01", "2026-10-16"]})
        self.assertEqual(doc["champions"][0]["spec"]["name"], "orb-15m-mid-1R")
        self.assertEqual(run(["init"])[0], 1)   # no silent reset of the experiment count

    def test_diagnose_reads_development_only(self):
        run(["init"])
        code, out, _ = run(["diagnose", "--json"])
        doc = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(doc["trades"])
        self.assertTrue(all(t["date"] <= "2026-09-14" for t in doc["trades"]))

    def test_test_logs_and_refuses_a_repeat(self):
        run(["init", "--min-trades", "5"])
        p = self.spec("skip-wide", max_or_pct=2.0)
        code, out, _ = run(["test", p, "--note", "wide ranges stop out"])
        self.assertEqual(code, 2, out)               # the reel's data does not support it
        self.assertIn("KEEP CHAMPION v1", out)
        self.assertEqual(len(lab.experiments()), 1)
        code, _, err = run(["test", self.spec("same-idea", max_or_pct=2.0)])
        self.assertEqual(code, 1)
        self.assertIn("already tested as experiment #1", err)
        self.assertEqual(run(["promote", "1"])[0], 1)
        self.assertEqual(len(lab.load_lab()["champions"]), 1)

    def test_no_test_reads_the_holdout(self):
        run(["init", "--min-trades", "5"])
        run(["test", self.spec("t2", target_r=2.0), "--json"])
        exp = lab.experiments()[0]
        for w in exp["result"]["windows"]:
            self.assertLessEqual(w["to"], "2026-09-30")

    def test_promote_needs_a_pass_against_the_current_champion(self):
        run(["init", "--min-trades", "1"])
        run(["test", self.spec("t2", target_r=2.0)])
        exp = lab.experiments()[0]
        exp["result"]["promotable"] = True           # pretend it passed
        with open(lab.lab_path("experiments.jsonl"), "w") as fh:
            fh.write(json.dumps(exp) + "\n")
        code, out, _ = run(["promote", "1"])
        self.assertEqual(code, 0, out)
        self.assertEqual(lab.load_lab()["champions"][-1]["version"], 2)
        self.assertEqual(run(["promote", "1"])[0], 1)   # tested against v1, the champion is v2

    def test_holdout_runs_once_per_version(self):
        run(["init"])
        code, out, _ = run(["holdout"])
        self.assertEqual(code, 0)
        self.assertIn("2026-10-01 to 2026-10-16", out)
        first = lab.load_lab()["champions"][-1]["holdout"]
        code, out, _ = run(["holdout"])
        self.assertIn("already run on the holdout", out)
        self.assertEqual(lab.load_lab()["champions"][-1]["holdout"], first)

    def test_check_reads_todays_range(self):
        run(["init"])
        code, out, _ = run(["check", "TSLA", "--offline", TM_DIR, "--now", "2026-10-19 10:20"])
        self.assertEqual(code, 0, out)
        self.assertIn("opening range 354.47 to 362.18", out)

    def test_show_hands_off_to_entry_and_exit(self):
        run(["init"])
        code, out, _ = run(["show"])
        self.assertEqual(code, 0)
        self.assertIn("entry.py check SYMBOL --setup orb --or-minutes 15", out)
        self.assertIn("exit.py plan --or-minutes 15 --stop or-mid --target 1R --write", out)


if __name__ == "__main__":
    unittest.main()
