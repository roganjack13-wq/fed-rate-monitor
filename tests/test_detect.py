"""detect.py（變化偵測狀態機）、attribute.py、messages.py 的單元測試。全部用合成資料，不連網。"""
import copy
import datetime as dt
import unittest

from fw import attribute, detect, messages
from fw import timeutil as tu

CFG = {
    "local_utc_offset_hours": 8,
    "alert": {"meetings": 2, "threshold_pp": 5.0, "flip_margin_pp": 2.0, "event_threshold_pp": 2.0,
              "event_after_min": 10, "event_until_min": 45, "confirm_polls": 2, "cooldown_min": 60,
              "max_per_day": 8, "cross_check_bp": 2.0, "fail_after": 3},
    "summary": {"enabled": True, "time": "07:30", "weekdays": [1, 2, 3, 4, 5]},
    "quiet_hours": {"start": "00:00", "end": "07:00", "mode": "silent"},
}
T0 = dt.datetime(2030, 2, 12, 15, 2, tzinfo=tu.UTC)      # 週二，交易時段
NEAR, NEXT = "2030-03-20", "2030-06-12"


def minutes(n):
    return T0 + dt.timedelta(minutes=n)


def snap(t, p_near, p_next=0.5, trade=None, skip=()):
    """最近一場：升 1 碼的機率 p_near；下一場：累計升 1 碼的機率 p_next。trade：最新成交時間。"""
    m = {NEAR: {"400": round(1 - p_near, 4), "425": round(p_near, 4)},
         NEXT: {"400": round(1 - p_next, 4), "425": round(p_next, 4)}}
    tr = int((trade or t).timestamp())
    return {"t": tu.iso(t), "b": 400, "m": m, "s": list(skip),
            "q": {d: tr for d in m}, "g": {d: str(tr) for d in m}}


def event(t, title="CPI m/m", impact="High"):
    return {"id": "ff|%s|%s" % (tu.iso(t), title), "t": tu.iso(t), "title": title, "impact": impact,
            "forecast": "0.3%", "previous": "0.6%"}


class Sim:
    def __init__(self, cfg=None):
        self.cfg = cfg or CFG
        self.state = {}
        self.rows = []

    def find_ref(self, t):
        key = tu.iso(t)
        prior = [r for r in self.rows if r["t"] <= key]
        return prior[-1] if prior else None

    def step(self, s, events=()):
        now = tu.parse_iso(s["t"])
        res = detect.evaluate(self.state, s, self.find_ref, list(events), now, self.cfg)
        self.rows.append(s)
        if res["alert"]:
            detect.commit_alert(self.state, res["alert"], s, now, self.cfg)
        return res


class Threshold(unittest.TestCase):
    def test_first_run_only_sets_baseline(self):
        sim = Sim()
        res = sim.step(snap(minutes(0), 0.60))
        self.assertIsNone(res["alert"])
        self.assertEqual(res["reset"], "first")
        self.assertEqual(sim.state["baseline"]["m"][NEAR]["425"], 0.60)

    def test_small_moves_do_not_alert(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        for i, p in enumerate((0.62, 0.63, 0.58, 0.64), 1):
            self.assertIsNone(sim.step(snap(minutes(10 * i), p))["alert"])
        self.assertIsNone(sim.state.get("pending"))

    def test_needs_two_polls_with_a_new_trade(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        self.assertIsNone(sim.step(snap(minutes(10), 0.66))["alert"])          # 第一次看到
        res = sim.step(snap(minutes(20), 0.67))                                # 有新成交、仍達標
        self.assertEqual(res["alert"]["kind"], "threshold")
        self.assertEqual(res["alert"]["ref_t"], tu.iso(minutes(0)))
        self.assertEqual(sim.state["baseline"]["m"][NEAR]["425"], 0.67)

    def test_same_price_without_new_trade_is_not_confirmation(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        stale_trade = minutes(10)
        for i in range(1, 6):                                                  # 價格沒動、也沒有新成交
            self.assertIsNone(sim.step(snap(minutes(10 * i), 0.66, trade=stale_trade))["alert"])
        res = sim.step(snap(minutes(60), 0.66))                                # 出現新成交
        self.assertIsNotNone(res["alert"])

    def test_move_that_reverts_is_dropped(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        sim.step(snap(minutes(10), 0.66))
        self.assertIsNone(sim.step(snap(minutes(20), 0.61))["alert"])
        self.assertIsNone(sim.state.get("pending"))
        self.assertIsNone(sim.step(snap(minutes(30), 0.66))["alert"])          # 重新從第一次算起

    def test_big_move_alerts_immediately(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        res = sim.step(snap(minutes(10), 0.71))
        self.assertEqual(res["alert"]["kind"], "threshold")
        self.assertAlmostEqual(res["alert"]["moved_pp"], 11.0)

    def test_cooldown_and_breakthrough(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        self.assertIsNotNone(sim.step(snap(minutes(10), 0.71))["alert"])       # 通知 → 基準 0.71
        self.assertIsNone(sim.step(snap(minutes(20), 0.77))["alert"])
        self.assertIsNone(sim.step(snap(minutes(30), 0.78))["alert"])          # 已確認，但還在冷卻
        self.assertIsNone(sim.step(snap(minutes(60), 0.78))["alert"])
        self.assertIsNotNone(sim.step(snap(minutes(75), 0.78))["alert"])       # 冷卻結束
        self.assertIsNotNone(sim.step(snap(minutes(85), 0.90))["alert"])       # 變動超過門檻 2 倍，不受冷卻限制

    def test_flip_of_most_likely_outcome(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.49))
        self.assertIsNone(sim.step(snap(minutes(10), 0.52))["alert"])
        res = sim.step(snap(minutes(20), 0.52))
        self.assertEqual(res["alert"]["kind"], "flip")

    def test_far_meeting_uses_bucket_change_only(self):
        ref = {"400": 0.12, "425": 0.65, "450": 0.23}
        cur = {"400": 0.131, "425": 0.679, "450": 0.19}
        self.assertAlmostEqual(detect.move_pp(cur, ref, nearest=False), 4.0, places=3)
        self.assertGreater(detect.move_pp(cur, ref, nearest=True), 4.8)

    def test_crossing_an_integer_on_nearest_meeting(self):
        ref = {"400": 0.03, "425": 0.97}
        cur = {"425": 0.97, "450": 0.03}
        self.assertAlmostEqual(detect.move_pp(cur, ref, nearest=True), 6.0, places=3)

    def test_meeting_with_bad_data_is_not_watched(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60, skip=[NEAR]))
        res = sim.step(snap(minutes(10), 0.90, skip=[NEAR]))
        self.assertIsNone(res["alert"])

    def test_month_and_meeting_rollover_reset_baseline(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        march = dt.datetime(2030, 3, 4, 15, 2, tzinfo=tu.UTC)
        res = sim.step(snap(march, 0.90))
        self.assertEqual(res["reset"], "month")
        self.assertIsNone(res["alert"])
        s = snap(march + dt.timedelta(minutes=10), 0.90)
        del s["m"][NEAR]                                                       # 最近一場會議開完了
        res = sim.step(s)
        self.assertEqual(res["reset"], "meeting")
        self.assertIsNone(res["alert"])

    def test_daily_cap(self):
        cfg = copy.deepcopy(CFG)
        cfg["alert"]["max_per_day"] = 2
        cfg["alert"]["cooldown_min"] = 0
        sim = Sim(cfg)
        sim.step(snap(minutes(0), 0.10))
        self.assertIsNotNone(sim.step(snap(minutes(10), 0.30))["alert"])
        self.assertIsNotNone(sim.step(snap(minutes(20), 0.50))["alert"])
        res = sim.step(snap(minutes(30), 0.70))
        self.assertIsNone(res["alert"])
        self.assertTrue(res["capped"])


class Events(unittest.TestCase):
    def setUp(self):
        self.te = minutes(28)                                                  # 數據公布時間
        self.ev = [event(self.te), event(self.te, "Core CPI m/m")]

    def test_reaction_after_release(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        sim.step(snap(minutes(10), 0.61), self.ev)
        sim.step(snap(minutes(20), 0.61), self.ev)                             # 公布前最後一筆
        self.assertIsNone(sim.step(snap(minutes(30), 0.61, trade=minutes(27)), self.ev)["alert"])
        res = sim.step(snap(minutes(40), 0.66), self.ev)                       # 公布後 12 分鐘
        self.assertEqual(res["alert"]["kind"], "event")
        self.assertEqual(res["alert"]["ref_t"], tu.iso(minutes(20)))
        self.assertEqual(len(res["alert"]["events"]), 2)
        self.assertIsNone(sim.step(snap(minutes(50), 0.66), self.ev)["alert"])  # 同一個事件不重發

    def test_needs_a_trade_after_the_release(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        sim.step(snap(minutes(20), 0.60), self.ev)
        res = sim.step(snap(minutes(40), 0.64, trade=minutes(25)), self.ev)    # 報價還停在公布前
        self.assertIsNone(res["alert"])

    def test_small_reaction_needs_confirmation(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        sim.step(snap(minutes(20), 0.60), self.ev)
        self.assertIsNone(sim.step(snap(minutes(40), 0.625), self.ev)["alert"])
        res = sim.step(snap(minutes(50), 0.63), self.ev)
        self.assertEqual(res["alert"]["kind"], "event")

    def test_no_reaction_expires(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        for i in range(1, 9):
            self.assertIsNone(sim.step(snap(minutes(10 * i), 0.605), self.ev)["alert"])
        self.assertIn(self.ev[0]["id"], sim.state["events_done"])

    def test_threshold_alert_during_window_carries_the_event(self):
        sim = Sim()
        sim.step(snap(minutes(0), 0.60))
        sim.step(snap(minutes(20), 0.60), self.ev)
        res = sim.step(snap(minutes(33), 0.75), self.ev)                       # 公布後 5 分鐘就大幅變動
        self.assertEqual(res["alert"]["kind"], "event")
        self.assertEqual(len(res["alert"]["events"]), 2)
        self.assertIsNone(sim.step(snap(minutes(43), 0.76), self.ev)["alert"])  # 不會再為同一個事件發一次

    def test_low_impact_events_do_not_trigger(self):
        sim = Sim()
        ev = [event(self.te, "FOMC Member Waller Speaks", "Low")]
        sim.step(snap(minutes(0), 0.60))
        sim.step(snap(minutes(20), 0.60), ev)
        self.assertIsNone(sim.step(snap(minutes(40), 0.63), ev)["alert"])


class Housekeeping(unittest.TestCase):
    def test_summary_due(self):
        state = {}
        tue_0725 = dt.datetime(2030, 2, 11, 23, 25, tzinfo=tu.UTC)             # 台北週二 07:25
        self.assertIsNone(detect.summary_due(state, tue_0725, CFG))
        tue_0731 = tue_0725 + dt.timedelta(minutes=6)
        self.assertEqual(detect.summary_due(state, tue_0731, CFG), "2030-02-12")
        state["summary_date"] = "2030-02-12"
        self.assertIsNone(detect.summary_due(state, tue_0731, CFG))
        mon = dt.datetime(2030, 2, 10, 23, 40, tzinfo=tu.UTC)                  # 台北週一：不發
        self.assertIsNone(detect.summary_due({}, mon, CFG))
        late = dt.datetime(2030, 2, 12, 7, 0, tzinfo=tu.UTC)                   # 台北週二 15:00：太晚，不補發
        st = {}
        self.assertIsNone(detect.summary_due(st, late, CFG))
        self.assertEqual(st["summary_date"], "2030-02-12")

    def test_quiet_hours(self):
        self.assertTrue(detect.in_quiet_hours(dt.datetime(2030, 2, 11, 19, 0, tzinfo=tu.UTC), CFG))   # 台北 03:00
        self.assertFalse(detect.in_quiet_hours(dt.datetime(2030, 2, 12, 1, 0, tzinfo=tu.UTC), CFG))   # 台北 09:00

    def test_failure_counter(self):
        state = {}
        self.assertFalse(detect.note_failure(state, CFG))
        self.assertFalse(detect.note_failure(state, CFG))
        self.assertTrue(detect.note_failure(state, CFG))
        state["fail_alerted"] = True
        self.assertFalse(detect.note_failure(state, CFG))
        self.assertTrue(detect.note_success(state))
        self.assertEqual(state["fail_n"], 0)


class Attribution(unittest.TestCase):
    def test_causes_and_message(self):
        te = minutes(28)
        cal = [event(te), event(te, "Core CPI m/m"),
               event(minutes(5), "FOMC Member Waller Speaks", "Low"),
               event(minutes(-400), "PPI m/m")]
        fed = [{"t": tu.iso(minutes(5)), "title": "Waller, Outlook <test>", "link": "https://example.org/a?x=1&y=2"}]
        news = [{"t": tu.iso(minutes(32)), "title": "美國 CPI 高於預期", "link": "https://example.org/n", "source": "測試社", "lang": "zh"},
                {"t": tu.iso(minutes(33)), "title": "天氣預報", "link": "https://example.org/w", "source": "測試社"},
                {"t": tu.iso(minutes(-300)), "title": "Fed 舊聞", "link": "https://example.org/o", "source": "測試社"}]
        causes = attribute.find_causes(cal, fed, news, minutes(0), minutes(40), [cal[0], cal[1]])
        self.assertEqual([e["title"] for e in causes["events"]], ["CPI m/m", "Core CPI m/m"])   # 談話已有官方連結
        self.assertEqual(len(causes["fed"]), 1)
        self.assertEqual([n["title"] for n in causes["news"]], ["美國 CPI 高於預期"])

        cur = snap(minutes(40), 0.66)
        alert = {"kind": "event", "t": tu.iso(minutes(40)), "ref_t": tu.iso(minutes(20)),
                 "ref": snap(minutes(20), 0.60)["m"], "events": [cal[0]], "watch": [NEAR, NEXT], "moved_pp": 6.0}
        text = messages.compose_change(alert, cur, causes, CFG)
        self.assertIn("升息 1 碼 4.25–4.50%：60.0% → 66.0%（+6.0）", text)
        self.assertIn("美國 CPI 月增率（預期 0.3%、前值 0.6%）", text)
        self.assertIn("Outlook &lt;test&gt;", text)                             # 外來文字有跳脫
        self.assertIn("x=1&amp;y=2", text)
        self.assertIn("不構成投資建議", text)
        self.assertNotIn(NEXT[5:], text.split("可能原因")[0].split("▍")[-1] if "6/12" not in text else "")

    def test_nothing_matches(self):
        causes = attribute.find_causes([event(minutes(-60), "FOMC Member Logan Speaks", "Low")], [], [],
                                       minutes(0), minutes(40))
        self.assertEqual(causes["events"], [])
        self.assertEqual([e["title"] for e in causes["earlier"]], ["FOMC Member Logan Speaks"])
        alert = {"kind": "threshold", "t": tu.iso(minutes(40)), "ref_t": tu.iso(minutes(0)),
                 "ref": snap(minutes(0), 0.60)["m"], "events": [], "watch": [NEAR], "moved_pp": 6.0}
        text = messages.compose_change(alert, snap(minutes(40), 0.66), causes, CFG)
        self.assertIn("稍早有", text)
        self.assertIn("聯準會官員 Logan 談話", text)
        text2 = messages.compose_change(alert, snap(minutes(40), 0.66), {"events": [], "fed": [], "news": []}, CFG)
        self.assertIn("沒有排定的重要數據或官員談話", text2)

    def test_labels(self):
        self.assertEqual(messages.bucket_range("375"), "3.75–4.00%")
        self.assertEqual(messages.move_label("425", 375, False), "升息 2 碼")
        self.assertEqual(messages.move_label("350", 375, False), "降息 1 碼")
        self.assertEqual(messages.move_label("375", 375, True), "與目前相同")
        self.assertEqual(messages.move_label("425", 375, True), "累計升 2 碼")
        self.assertEqual(attribute.zh_title("FOMC Member Bowman Speaks"), "聯準會官員 Bowman 談話")
        self.assertEqual(attribute.zh_title("Something New"), "Something New")

    def test_summary_message(self):
        cur = {NEAR: {"400": 0.10, "425": 0.90}, NEXT: {"400": 0.05, "425": 0.50, "450": 0.45}}
        prev = {NEAR: {"400": 0.16, "425": 0.84}, NEXT: {"400": 0.12, "425": 0.68, "450": 0.20}}
        text = messages.compose_summary("2030-02-11", cur, prev, "2030-02-08", 400, False, [],
                                        [event(minutes(600), "PPI m/m")], minutes(0), CFG)
        self.assertIn("每日總結｜2/11（一）定盤值", text)
        self.assertIn("最可能：升息 1 碼 4.25–4.50%　90.0%（前一交易日 84.0%，+6.0）", text)
        self.assertIn("沒有達到通知門檻的變動", text)
        self.assertIn("PPI 月增率", text)


if __name__ == "__main__":
    unittest.main()
