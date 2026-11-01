"""Run with:  python3 -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_events as fe  # noqa: E402
from fakes import FakeDiscoveryApi, tm_event  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

CONFIG = {
    "watchlist": ["Demo Singer", {"name": "Sample Band"}, "Nobody Here"],
    "center": {"name": "Nashville, TN", "lat": 36.1627, "lon": -86.7816},
    "radius_miles": 30,
    "nearby_radius_miles": 350,
    "months_ahead": 12,
    "major_venues": ["Bridgestone Arena", "Ryman Auditorium", "FirstBank Amphitheater"],
    "exclude_name_patterns": ["parking", "vip package"],
    "manual_events": [
        {"name": "Sample Band Live", "artists": ["Sample Band"], "date": "2027-05-02", "venue": "Somewhere"},
        {"name": "Garage Show", "artists": ["Local Hero"], "date": "2026-12-05", "time": "20:00",
         "venue": "The Basement East", "note": "官宣"},
        {"name": "Old Show", "date": "2026-10-01"},
        {"name": "Bad Date", "date": "next friday"},
    ],
}


def demo_api(extra_artist_events=()):
    star = [("A1", "Demo Singer")]
    headline = tm_event("E1", "Demo Singer: World Tour", "2027-03-14", attractions=star,
                        price=(49.5, 199), onsale="2026-11-07T16:00:00Z",
                        presales=[("Fan Presale", "2026-11-05T15:00:00Z", "2026-11-06T04:00:00Z")])
    local = [
        headline,
        tm_event("E1-dup", "Demo Singer: World Tour - Official Resale", "2027-03-14", attractions=star),
        tm_event("P1", "PARKING: Demo Singer: World Tour", "2027-03-14", attractions=star),
        tm_event("E2", "Opry Night", "2026-11-20", venue="ryman", genre="Country"),
        tm_event("E3", "Indie Act", "2026-11-15", venue="basement", attractions=[("B1", "Indie Act")],
                 genre="Alternative"),
        tm_event("E4", "The Sample Band", "2027-05-02", venue="firstbank",
                 attractions=[("C1", "The Sample Band")], genre="Rock"),
        tm_event("E5", "Mystery Night", "2026-12-01", venue="basement", time_tba=True),
    ]
    artist_events = {
        "A1": [headline,
               tm_event("E6", "Demo Singer: World Tour", "2027-03-18", venue="atlanta", attractions=star),
               tm_event("E7", "Demo Singer: World Tour", "2027-06-01", venue="london", attractions=star),
               *extra_artist_events],
    }
    artists = {"Demo Singer": [("A1", "Demo Singer"), ("A9", "Demo Singer Tribute")]}
    return FakeDiscoveryApi(local, artists, artist_events)


def client_for(api):
    return fe.Ticketmaster("secret-key", fetch=api, min_interval=0, sleep=lambda s: None)


class Helpers(unittest.TestCase):
    def test_geohash_matches_reference(self):
        self.assertEqual(fe.geohash(57.64911, 10.40744, 11), "u4pruydqqvj")

    def test_norm(self):
        self.assertEqual(fe.norm("The Weeknd"), "weeknd")
        self.assertEqual(fe.norm("Beyoncé"), "beyonce")
        self.assertEqual(fe.norm("Mumford & Sons"), fe.norm("Mumford and Sons"))
        self.assertEqual(fe.norm("周杰伦"), "周杰伦")

    def test_ics_lines_are_folded(self):
        events = [{"id": "x", "name": "很长的演出名" * 20, "date": "2027-01-02", "watch": ["A"],
                   "first_seen": "2026-11-01", "url": "https://example.com/" + "a" * 120}]
        ics = fe.build_ics(events)
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75)
        self.assertIn("DTSTART;VALUE=DATE:20270102", ics)


class ClientBehaviour(unittest.TestCase):
    def test_splits_windows_over_the_1000_result_cap(self):
        start = NOW
        events = [tm_event(f"X{i}", f"Show {i}", (start + timedelta(hours=5 * i)).date().isoformat(),
                           local_time="20:00") for i in range(1500)]
        api = FakeDiscoveryApi(events)
        got = client_for(api).search_events({"geoPoint": "dn6m"}, start, start + timedelta(days=366))
        self.assertEqual({e["id"] for e in got}, {e["id"] for e in events})

    def test_retries_rate_limit_then_succeeds(self):
        calls = {"n": 0}

        def flaky(url):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError(url, 429, "slow down", None, None)
            return {"page": {"totalElements": 0, "totalPages": 0}}

        self.assertEqual(client_for(flaky).search_events({}, NOW, NOW + timedelta(days=1)), [])
        self.assertEqual(calls["n"], 3)

    def test_bad_key_is_reported_without_leaking_it(self):
        with self.assertRaises(fe.ApiError) as ctx:
            client_for(FakeDiscoveryApi(fail_with=401)).get("events.json", {})
        self.assertIn("401", str(ctx.exception))
        with self.assertRaises(fe.ApiError) as ctx:
            client_for(FakeDiscoveryApi(fail_with=404)).get("events.json", {})
        self.assertNotIn("secret-key", str(ctx.exception))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        self.outputs = self.root / "gh_output"
        env = {"GITHUB_OUTPUT": str(self.outputs), "GITHUB_REPOSITORY": "someone/nashville-concerts"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_once(self, api, now=NOW):
        self.outputs.write_text("")
        with mock.patch("sys.stdout"):
            code = fe.main(["--root", str(self.root)], client=client_for(api), now=now)
        self.assertEqual(code, 0)
        data = json.loads((self.root / "data/events.json").read_text(encoding="utf-8"))
        outputs = dict(line.split("=") for line in self.outputs.read_text().split())
        return data, {e["id"]: e for e in data["events"]}, outputs

    def notify(self):
        title = self.root / "build/notify_title.txt"
        if not title.exists():
            return None, None
        return title.read_text(encoding="utf-8"), (self.root / "build/notify.md").read_text(encoding="utf-8")

    def test_full_pipeline(self):
        data, by_id, outputs = self.run_once(demo_api())
        manual = [e for e in data["events"] if e["source"] == "manual"]

        # Parking listings, resale duplicates, TBA-less leftovers and far-away dates are dropped.
        self.assertNotIn("P1", by_id)
        self.assertNotIn("E1-dup", by_id)
        self.assertNotIn("E7", by_id)
        # Manual: duplicate of a Ticketmaster show, past and malformed entries are dropped.
        self.assertEqual([e["name"] for e in manual], ["Garage Show"])

        headline = by_id["E1"]
        self.assertEqual(headline["watch"], ["Demo Singer"])
        self.assertEqual(headline["area"], "nashville")
        self.assertTrue(headline["major"])
        self.assertEqual(headline["price"], {"min": 49.5, "max": 199, "currency": "USD"})
        self.assertEqual(headline["onsale"], "2026-11-07T16:00:00Z")
        self.assertEqual(headline["presales"][0]["name"], "Fan Presale")
        self.assertTrue(headline["image"].endswith("_640.jpg"))
        self.assertEqual(headline["time"], "19:30")

        atlanta = by_id["E6"]
        self.assertEqual((atlanta["area"], atlanta["watch"], atlanta["city"]), ("nearby", ["Demo Singer"], "Atlanta"))
        self.assertGreater(atlanta["distance_mi"], 150)

        self.assertEqual(by_id["E4"]["watch"], ["Sample Band"])  # matched by name, "The" ignored
        self.assertTrue(by_id["E4"]["major"])
        self.assertNotIn("watch", by_id["E2"])
        self.assertNotIn("major", by_id["E3"])
        self.assertNotIn("time", by_id["E5"])
        self.assertEqual([e["date"] for e in data["events"]], sorted(e["date"] for e in data["events"]))

        self.assertEqual(data["watchlist"], [{"name": "Demo Singer", "found": True},
                                             {"name": "Sample Band", "found": False},
                                             {"name": "Nobody Here", "found": False}])
        self.assertEqual(data["tracking_since"], "2026-11-01")
        self.assertEqual(outputs, {"changed": "true", "notify": "true"})

        title, body = self.notify()
        self.assertTrue(title.startswith("🎤 开始追踪"))
        self.assertIn("### 纳什维尔（2 场）", body)
        self.assertIn("### 周边城市（1 场）", body)
        self.assertIn("开售 11-07 10:00", body)
        self.assertIn("https://someone.github.io/nashville-concerts/", body)

        ics = (self.root / "data/watchlist.ics").read_text(encoding="utf-8")
        self.assertEqual(ics.count("BEGIN:VEVENT"), 3)
        self.assertIn("UID:E6@nashville-concerts", ics)

        # Same data again: nothing new to commit or announce, first_seen is kept.
        later = NOW + timedelta(days=2)
        data2, by_id2, outputs2 = self.run_once(demo_api(), now=later)
        self.assertEqual(outputs2, {"changed": "false", "notify": "false"})
        self.assertEqual(self.notify(), (None, None))
        self.assertEqual(by_id2["E1"]["first_seen"], "2026-11-01")

        # A newly announced Nashville date triggers exactly one notification.
        extra = tm_event("E8", "Demo Singer: World Tour", "2027-03-15", attractions=[("A1", "Demo Singer")])
        data3, by_id3, outputs3 = self.run_once(demo_api([extra]), now=later + timedelta(days=1))
        self.assertEqual(outputs3, {"changed": "true", "notify": "true"})
        self.assertEqual(by_id3["E8"]["first_seen"], "2026-11-04")
        title, body = self.notify()
        self.assertEqual(title, "🎤 关注歌手新演出：Demo Singer")
        self.assertIn("2027-03-15（周一）", body)
        self.assertNotIn("2027-03-14", body)

    def test_api_failure_keeps_existing_data(self):
        with mock.patch("sys.stdout"):
            code = fe.main(["--root", str(self.root)], client=client_for(FakeDiscoveryApi(fail_with=401)), now=NOW)
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "data/events.json").exists())

    def test_missing_key_is_a_no_op(self):
        with mock.patch.dict(os.environ, {"TICKETMASTER_API_KEY": ""}), mock.patch("sys.stdout"):
            code = fe.main(["--root", str(self.root)], now=NOW)
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "data").exists())
        self.assertIn("changed=false", self.outputs.read_text())


if __name__ == "__main__":
    unittest.main()
