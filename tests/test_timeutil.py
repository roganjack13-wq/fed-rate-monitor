"""timeutil.py、store.py、sources.py 解析函式的單元測試（不連網）。"""
import datetime as dt
import unittest

from fw import sources, store
from fw import timeutil as tu


def utc(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=tu.UTC)


class TimeZones(unittest.TestCase):
    def test_settle_time_winter_and_summer(self):
        self.assertEqual(tu.settle_utc(dt.date(2026, 11, 10)), utc(2026, 11, 10, 20, 0))   # 標準時間 UTC-6
        self.assertEqual(tu.settle_utc(dt.date(2026, 7, 1)), utc(2026, 7, 1, 19, 0))       # 夏令時間 UTC-5

    def test_decision_time(self):
        self.assertEqual(tu.decision_utc(dt.date(2026, 12, 9)), utc(2026, 12, 9, 19, 0))
        self.assertEqual(tu.decision_utc(dt.date(2027, 6, 9)), utc(2027, 6, 9, 18, 0))

    def test_dst_boundaries_2026(self):
        # 2026 年夏令時間：3/8 開始、11/1 結束
        self.assertEqual(tu.us_offset(utc(2026, 3, 8, 7, 59), tu.CT_STD), -6)
        self.assertEqual(tu.us_offset(utc(2026, 3, 8, 8, 0), tu.CT_STD), -5)
        self.assertEqual(tu.us_offset(utc(2026, 11, 1, 6, 59), tu.CT_STD), -5)
        self.assertEqual(tu.us_offset(utc(2026, 11, 1, 7, 0), tu.CT_STD), -6)

    def test_round_trip(self):
        for d in (utc(2026, 1, 15, 3, 30), utc(2026, 7, 4, 22, 5)):
            self.assertEqual(tu.from_zone(tu.to_ct(d), tu.CT_STD), d)
            self.assertEqual(tu.from_zone(tu.to_et(d), tu.ET_STD), d)

    def test_parse_iso(self):
        self.assertEqual(tu.parse_iso("2026-11-10T08:30:00-05:00"), utc(2026, 11, 10, 13, 30))
        self.assertEqual(tu.parse_iso("2026-11-10T13:30:00Z"), utc(2026, 11, 10, 13, 30))
        self.assertEqual(tu.iso(utc(2026, 11, 10, 13, 30)), "2026-11-10T13:30:00Z")

    def test_cme_hours(self):
        self.assertTrue(tu.cme_open(utc(2026, 11, 10, 15, 0)))     # 週二 09:00 CT
        self.assertFalse(tu.cme_open(utc(2026, 11, 10, 22, 30)))   # 週二 16:30 CT 每日休息
        self.assertTrue(tu.cme_open(utc(2026, 11, 10, 23, 5)))     # 週二 17:05 CT
        self.assertFalse(tu.cme_open(utc(2026, 11, 13, 22, 30)))   # 週五 16:30 CT 已收盤
        self.assertFalse(tu.cme_open(utc(2026, 11, 14, 15, 0)))    # 週六
        self.assertFalse(tu.cme_open(utc(2026, 11, 15, 22, 0)))    # 週日 16:00 CT
        self.assertTrue(tu.cme_open(utc(2026, 11, 15, 23, 30)))    # 週日 17:30 CT

    def test_last_settle_day(self):
        self.assertEqual(tu.last_settle_day(utc(2026, 11, 10, 20, 30)), dt.date(2026, 11, 10))
        self.assertEqual(tu.last_settle_day(utc(2026, 11, 10, 20, 5)), dt.date(2026, 11, 9))
        self.assertEqual(tu.last_settle_day(utc(2026, 11, 15, 12, 0)), dt.date(2026, 11, 13))  # 週日 → 上週五

    def test_local_format(self):
        self.assertEqual(tu.fmt_local(utc(2026, 11, 10, 13, 30)), "11/10 21:30")


class Parsers(unittest.TestCase):
    def test_fomc_calendar(self):
        html = """
        <h4>2030 FOMC Meetings</h4>
        <div class="fomc-meeting__month col-xs-5"><strong>January</strong></div>
        <div class="fomc-meeting__date col-xs-4">29-30</div>
        <div class="fomc-meeting__month col-xs-5"><strong>March</strong></div>
        <div class="fomc-meeting__date col-xs-4">19-20*</div>
        <div class="fomc-meeting__month col-xs-5"><strong>Apr/May</strong></div>
        <div class="fomc-meeting__date col-xs-4">30-1</div>
        <div class="fomc-meeting__month col-xs-5"><strong>August</strong></div>
        <div class="fomc-meeting__date col-xs-4">22 (unscheduled)</div>
        <h4>2029 FOMC Meetings</h4>
        <div class="fomc-meeting__month col-xs-5"><strong>December</strong></div>
        <div class="fomc-meeting__date col-xs-4">11-12*</div>
        """
        self.assertEqual(sources.parse_fomc_calendar(html),
                         ["2029-12-12", "2030-01-30", "2030-03-20", "2030-05-01"])

    def test_rss(self):
        xml = b"""<?xml version="1.0"?><rss><channel>
        <item><title>Fed holds rates steady - Example News</title><link>https://example.com/a</link>
        <pubDate>Tue, 10 Nov 2026 13:31:00 GMT</pubDate><source url="https://example.com">Example News</source></item>
        <item><title>no date</title><link>https://example.com/b</link></item>
        </channel></rss>"""
        items = sources.parse_rss(xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Fed holds rates steady")
        self.assertEqual(items[0]["source"], "Example News")
        self.assertEqual(items[0]["t"], "2026-11-10T13:31:00Z")
        self.assertEqual(sources.parse_rss(b"not xml"), [])


class Retention(unittest.TestCase):
    def test_compact_intraday(self):
        now = utc(2026, 11, 20, 0, 0)
        rows = []
        t = utc(2026, 8, 1, 0, 0)
        while t < now:
            rows.append({"t": tu.iso(t)})
            t += dt.timedelta(minutes=10)
        kept = store.compact_intraday(rows, now)
        recent = [r for r in kept if tu.parse_iso(r["t"]) >= now - dt.timedelta(days=7)]
        older = [r for r in kept if tu.parse_iso(r["t"]) < now - dt.timedelta(days=7)]
        self.assertEqual(len(recent), 7 * 24 * 6)
        self.assertEqual(len(older), 83 * 24)
        self.assertGreaterEqual(tu.parse_iso(kept[0]["t"]), now - dt.timedelta(days=90))


if __name__ == "__main__":
    unittest.main()
