"""model.py 的單元測試（合成數字，手算得出預期結果）。執行：python -m unittest discover -s tests -t ."""
import datetime as dt
import unittest

from fw import model
from fw import timeutil as tu


def near(test, got, want, places=4):
    test.assertEqual(sorted(got), sorted(want))
    for k in want:
        test.assertAlmostEqual(got[k], want[k], places=places, msg="bucket %s" % k)


class SingleMeetings(unittest.TestCase):
    """三月、六月各一場，前後月份都沒有會議。"""

    def setUp(self):
        self.meetings = model.meeting_index(["2030-03-20", "2030-06-12"])
        self.asof = dt.datetime(2030, 2, 10, 12, 0, tzinfo=tu.UTC)
        self.prices = {
            (2030, 2): 96.00,    # 4.00%
            (2030, 3): 95.96,    # 會議當月合約：前後月都沒會議時用不到
            (2030, 4): 95.90,    # 4.10%
            (2030, 5): 95.90,    # 4.10%
            (2030, 6): 95.95,
            (2030, 7): 96.025,   # 3.975%
        }

    def test_distribution(self):
        res = model.compute(self.prices, self.meetings, self.asof, 400)
        self.assertEqual([r["date"] for r in res], ["2030-03-20", "2030-06-12"])
        self.assertAlmostEqual(res[0]["delta_bp"], 10.0, places=6)
        near(self, res[0]["dist"], {400: 0.6, 425: 0.4})
        self.assertAlmostEqual(res[1]["delta_bp"], -12.5, places=6)
        near(self, res[1]["dist"], {375: 0.3, 400: 0.5, 425: 0.2})

    def test_summary(self):
        res = model.compute(self.prices, self.meetings, self.asof, 400)
        s = model.summarize(res[1]["dist"], 400)
        self.assertAlmostEqual(s["up"], 0.2)
        self.assertAlmostEqual(s["hold"], 0.5)
        self.assertAlmostEqual(s["down"], 0.3)
        self.assertEqual(s["top"], 400)
        self.assertAlmostEqual(s["exp_steps"], -0.1)

    def test_stops_at_missing_price(self):
        prices = dict(self.prices)
        del prices[(2030, 7)]
        res = model.compute(prices, self.meetings, self.asof, 400)
        self.assertEqual(len(res), 1)

    def test_meeting_already_decided_is_skipped(self):
        after = tu.decision_utc(dt.date(2030, 3, 20)) + dt.timedelta(minutes=1)
        res = model.compute(self.prices, self.meetings, after, 400)
        self.assertEqual([r["date"] for r in res], ["2030-06-12"])

    def test_expired_previous_month_uses_settled_rate(self):
        asof = dt.datetime(2030, 3, 5, 12, 0, tzinfo=tu.UTC)
        prices = {k: v for k, v in self.prices.items() if k != (2030, 2)}
        res = model.compute(prices, self.meetings, asof, 400, settled={(2030, 2): 4.00})
        near(self, res[0]["dist"], {400: 0.6, 425: 0.4})
        self.assertFalse(res[0]["approx"])
        res2 = model.compute(prices, self.meetings, asof, 400, effr=4.00)
        near(self, res2[0]["dist"], {400: 0.6, 425: 0.4})
        self.assertTrue(res2[0]["approx"])


class ConsecutiveMeetingMonths(unittest.TestCase):
    """九月、十月連續兩個月都有會議：九月的會議後利率＝十月的會議前利率（由十月合約往回解）。"""

    def test_pair(self):
        meetings = model.meeting_index(["2030-09-18", "2030-10-30"])
        asof = dt.datetime(2030, 8, 5, 12, 0, tzinfo=tu.UTC)
        r_oct = (30 / 31) * 4.10 + (1 / 31) * 4.25
        prices = {(2030, 8): 96.00, (2030, 9): 95.95, (2030, 10): 100 - r_oct, (2030, 11): 95.75}
        res = model.compute(prices, meetings, asof, 400)
        self.assertAlmostEqual(res[0]["post"], 4.10, places=4)
        self.assertAlmostEqual(res[0]["delta_bp"], 10.0, places=3)
        self.assertAlmostEqual(res[1]["delta_bp"], 15.0, places=3)
        near(self, res[0]["dist"], {400: 0.6, 425: 0.4}, places=3)
        near(self, res[1]["dist"], {400: 0.24, 425: 0.52, 450: 0.24}, places=3)

    def test_previous_month_meeting_already_past(self):
        """九月的會議開完後看十月：十月的會議前利率要用十月合約往回解，不能直接拿九月合約。"""
        meetings = model.meeting_index(["2030-09-18", "2030-10-30"])
        asof = dt.datetime(2030, 9, 25, 12, 0, tzinfo=tu.UTC)
        r_oct = (30 / 31) * 4.10 + (1 / 31) * 4.25
        prices = {(2030, 9): 95.95, (2030, 10): 100 - r_oct, (2030, 11): 95.75}
        res = model.compute(prices, meetings, asof, 400)
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0]["pre"], 4.10, places=4)
        self.assertAlmostEqual(res[0]["delta_bp"], 15.0, places=3)


class Helpers(unittest.TestCase):
    def test_contract_code(self):
        self.assertEqual(model.contract_code((2026, 12)), "ZQZ26")
        self.assertEqual(model.contract_code((2027, 1)), "ZQF27")

    def test_required_contracts(self):
        meetings = model.meeting_index(["2030-03-20", "2030-06-12"])
        asof = dt.datetime(2030, 2, 10, 12, 0, tzinfo=tu.UTC)
        got = model.required_contracts(meetings, asof)
        self.assertEqual(got[0], (2030, 2))
        self.assertEqual(got[-1], (2030, 8))

    def test_monthly_average_carries_weekends(self):
        days = {dt.date(2030, 1, 31): 4.00}
        d = dt.date(2030, 2, 1)
        while d.month == 2:
            if d.weekday() < 5:
                days[d] = 4.00 if d.day < 15 else 4.25
            d += dt.timedelta(days=1)
        avg = model.monthly_average(days, (2030, 2))
        # 2030/2/15 是週五：1～14 日 4.00（14 天），15～28 日 4.25（14 天）
        self.assertAlmostEqual(avg, 4.125, places=4)
        self.assertIsNone(model.monthly_average({dt.date(2030, 2, 10): 4.0}, (2030, 2)))

    def test_infer_decision(self):
        self.assertEqual(model.infer_decision_steps(4.13, 3.88), 1)
        self.assertEqual(model.infer_decision_steps(3.89, 3.88), 0)
        self.assertEqual(model.infer_decision_steps(3.62, 3.88), -1)

    def test_post_rates(self):
        meetings = model.meeting_index(["2030-09-18", "2030-10-30"])
        asof = dt.datetime(2030, 8, 5, 12, 0, tzinfo=tu.UTC)
        r_oct = (30 / 31) * 4.10 + (1 / 31) * 4.25
        prices = {(2030, 10): 100 - r_oct, (2030, 11): 95.75}
        got = model.post_rates(prices, meetings, asof)
        self.assertAlmostEqual(got["2030-09-18"], 4.10, places=3)
        self.assertAlmostEqual(got["2030-10-30"], 4.25, places=3)


if __name__ == "__main__":
    unittest.main()
