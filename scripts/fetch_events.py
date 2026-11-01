#!/usr/bin/env python3
"""Fetch upcoming concerts in and around Nashville and write the site's data files.

Source: Ticketmaster Discovery API v2 (free key at https://developer.ticketmaster.com).

Reads   config.json          watchlist, radius, major venues, manual events
Writes  data/events.json     everything the web page renders
        data/watchlist.ics   subscribable calendar of watchlist artists' shows
        data/state.json      first-seen / notified bookkeeping between runs
        build/notify.md      only when watchlist artists have new shows; the
                             GitHub workflow turns it into an issue (= email)

Usage:
    TICKETMASTER_API_KEY=xxxx python3 scripts/fetch_events.py

Standard library only, so the workflow needs no pip install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - tzdata missing on the host
    LOCAL_TZ = timezone(timedelta(hours=-6))

API_BASE = "https://app.ticketmaster.com/discovery/v2"
PAGE_SIZE = 200
# The Discovery API refuses deep paging: size * page must stay below 1000.
MAX_RESULTS_PER_QUERY = 1000
# Forget ids of vanished events after this long (events are listed <= ~1 year ahead).
STATE_RETENTION_DAYS = 400
WEEKDAYS = "一二三四五六日"

DEFAULT_CONFIG = {
    "center": {"name": "Nashville, TN", "lat": 36.1627, "lon": -86.7816},
    "radius_miles": 30,
    "nearby_radius_miles": 350,
    "months_ahead": 12,
    "watchlist": [],
    "major_venues": [],
    "exclude_name_patterns": [],
    "manual_events": [],
}


class ApiError(RuntimeError):
    """The Ticketmaster API could not be queried successfully."""


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- helpers


def norm(text: str | None) -> str:
    """Loose name key: case/accent/punctuation-insensitive, leading 'the' dropped.

    Mirrors norm() in assets/app.js so both sides match names the same way.
    """
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ")
    s = re.sub(r"[\W_]+", " ", s).strip()
    return s[4:] if s.startswith("the ") else s


_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lon: float, precision: int = 9) -> str:
    """Encode a coordinate as a geohash (the API's geoPoint parameter)."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out, bits, ch, use_lon = [], 0, 0, True
    while len(out) < precision:
        if use_lon:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch, lon_lo = (ch << 1) | 1, mid
            else:
                ch, lon_hi = ch << 1, mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch, lat_lo = (ch << 1) | 1, mid
            else:
                ch, lat_hi = ch << 1, mid
        use_lon = not use_lon
        bits += 1
        if bits == 5:
            out.append(_GEOHASH_BASE32[ch])
            bits = ch = 0
    return "".join(out)


def miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(a))


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def embedded(payload: dict, key: str) -> list:
    return list((payload.get("_embedded") or {}).get(key) or [])


def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


# --------------------------------------------------------------------------- API client


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "nashville-concerts-tracker"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


class Ticketmaster:
    """Minimal Discovery API client with rate limiting, retries and deep-paging workaround."""

    def __init__(self, api_key: str, fetch=http_get_json, min_interval: float = 0.25,
                 max_retries: int = 4, sleep=time.sleep):
        self.api_key = api_key
        self.fetch = fetch
        self.min_interval = min_interval  # free tier: 5 requests / second
        self.max_retries = max_retries
        self.sleep = sleep
        self.calls = 0
        self._last = 0.0

    def get(self, path: str, params: dict) -> dict:
        query = {k: v for k, v in params.items() if v is not None}
        query.update(apikey=self.api_key, locale="*")
        url = f"{API_BASE}/{path}?{urllib.parse.urlencode(query)}"
        safe_url = url.replace(urllib.parse.quote_plus(self.api_key), "***")
        for attempt in range(self.max_retries + 1):
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                self.sleep(gap)
            self._last = time.monotonic()
            self.calls += 1
            try:
                return self.fetch(url)
            except urllib.error.HTTPError as err:
                if err.code == 401:
                    raise ApiError(
                        "Ticketmaster 拒绝了 API key（HTTP 401），请检查仓库 Secret TICKETMASTER_API_KEY。"
                    ) from None
                if not (err.code == 429 or err.code >= 500) or attempt == self.max_retries:
                    raise ApiError(f"HTTP {err.code}: {safe_url}") from None
            except (OSError, ValueError) as err:  # network trouble or a garbled body
                if attempt == self.max_retries:
                    raise ApiError(f"{type(err).__name__} ({err}): {safe_url}") from None
            self.sleep(min(2 ** attempt, 30))
        raise AssertionError("unreachable")

    def search_events(self, params: dict, start: datetime, end: datetime) -> list[dict]:
        """All events matching params in [start, end].

        The API only serves the first 1000 results of a query, so a window holding
        more than that is split in half until each piece fits.
        """
        base = {**params, "startDateTime": iso_utc(start), "endDateTime": iso_utc(end),
                "size": PAGE_SIZE, "sort": "date,asc"}
        first = self.get("events.json", {**base, "page": 0})
        page_info = first.get("page") or {}
        total = int(page_info.get("totalElements") or 0)
        if total > MAX_RESULTS_PER_QUERY and end - start > timedelta(hours=12):
            mid = (start + (end - start) / 2).replace(microsecond=0)
            return self.search_events(params, start, mid) + self.search_events(params, mid, end)
        events = embedded(first, "events")
        pages = min(int(page_info.get("totalPages") or 1), MAX_RESULTS_PER_QUERY // PAGE_SIZE)
        for page in range(1, pages):
            events += embedded(self.get("events.json", {**base, "page": page}), "events")
        return events


def find_attraction_ids(client: Ticketmaster, artist: dict) -> list[str]:
    """Ticketmaster attraction ids whose name matches the watchlist entry exactly."""
    if artist.get("tm_id"):
        ids = artist["tm_id"]
        return [ids] if isinstance(ids, str) else list(ids)
    wanted = {norm(artist["name"]), *(norm(a) for a in artist.get("aliases", []))}
    data = client.get("attractions.json",
                      {"keyword": artist["name"], "classificationName": "music", "size": 20})
    return [a["id"] for a in embedded(data, "attractions")
            if a.get("id") and norm(a.get("name")) in wanted]


# --------------------------------------------------------------------------- normalising


def _class_name(node) -> str | None:
    name = (node or {}).get("name")
    return None if not name or name.lower() in ("undefined", "other") else name


def _price(ranges) -> dict | None:
    ranges = [r for r in ranges or [] if isinstance(r.get("min"), (int, float))]
    if not ranges:
        return None
    lo = min(r["min"] for r in ranges)
    hi = max(r.get("max", r["min"]) for r in ranges)
    return {"min": round(lo, 2), "max": round(hi, 2), "currency": ranges[0].get("currency", "USD")}


def _image(images) -> str | None:
    """A 16:9 image around 640px wide: big enough for cards, small enough to load fast."""
    images = [i for i in images or [] if i.get("url")]
    wide = sorted((i for i in images if i.get("ratio") == "16_9" and (i.get("width") or 0) >= 300),
                  key=lambda i: i.get("width") or 0)
    if wide:
        return wide[0]["url"]
    return images[0]["url"] if images else None


def build_event(raw: dict, center: dict, exclude: list[re.Pattern], now: datetime) -> dict | None:
    """Flatten one Discovery API event; None for non-shows (parking, TBA dates, ...)."""
    name = (raw.get("name") or "").strip()
    if not name or not raw.get("id") or any(p.search(name) for p in exclude):
        return None
    dates = raw.get("dates") or {}
    start = dates.get("start") or {}
    if not start.get("localDate") or start.get("dateTBA") or start.get("dateTBD"):
        return None
    emb = raw.get("_embedded") or {}
    venue = (emb.get("venues") or [{}])[0]
    attractions = [a for a in emb.get("attractions") or [] if a.get("name")]
    loc = venue.get("location") or {}
    lat, lon = to_float(loc.get("latitude")), to_float(loc.get("longitude"))
    distance = (round(miles_between(center["lat"], center["lon"], lat, lon), 1)
                if lat is not None and lon is not None else None)
    no_time = bool(start.get("timeTBA") or start.get("noSpecificTime"))
    classes = raw.get("classifications") or [{}]
    cls = next((c for c in classes if c.get("primary")), classes[0])
    sales = raw.get("sales") or {}
    public = sales.get("public") or {}
    onsale = None if public.get("startTBD") else public.get("startDateTime")
    if onsale and (parse_utc(onsale) or now) <= now:
        onsale = None  # already on sale, nothing to announce
    presales = sorted(
        ({"name": p.get("name"), "start": p.get("startDateTime"), "end": p.get("endDateTime")}
         for p in sales.get("presales") or []
         if (parse_utc(p.get("endDateTime")) or now) > now and p.get("startDateTime")),
        key=lambda p: p["start"],
    )[:3]
    return {
        "id": raw["id"],
        "name": name,
        "artists": [a["name"] for a in attractions],
        "date": start["localDate"],
        "time": None if no_time else (start.get("localTime") or "")[:5] or None,
        "start_utc": None if no_time else start.get("dateTime"),
        "venue": venue.get("name"),
        "venue_id": venue.get("id"),
        "city": (venue.get("city") or {}).get("name"),
        "state": (venue.get("state") or {}).get("stateCode"),
        "distance_mi": distance,
        "genre": _class_name(cls.get("genre")),
        "subgenre": _class_name(cls.get("subGenre")),
        "price": _price(raw.get("priceRanges")),
        "status": (dates.get("status") or {}).get("code"),
        "onsale": onsale,
        "presales": presales,
        "url": raw.get("url"),
        "image": _image(raw.get("images")),
        "source": "ticketmaster",
        "_artist_ids": [a["id"] for a in attractions if a.get("id")],
    }


def build_manual_event(item: dict, center: dict, radius: float) -> dict | None:
    """An event typed into config.json's manual_events (for shows not on Ticketmaster)."""
    name = (item.get("name") or "").strip()
    day = str(item.get("date") or "")
    try:
        date.fromisoformat(day)
    except ValueError:
        log(f"  ! 跳过手动演出（缺少 name 或 date 格式不是 YYYY-MM-DD）：{item}")
        return None
    if not name:
        log(f"  ! 跳过手动演出（缺少 name）：{item}")
        return None
    lat, lon = to_float(item.get("lat")), to_float(item.get("lon"))
    distance = (round(miles_between(center["lat"], center["lon"], lat, lon), 1)
                if lat is not None and lon is not None else None)
    area = item.get("area") or ("nearby" if distance is not None and distance > radius else "nashville")
    key = f"{day}|{norm(name)}|{norm(item.get('venue'))}"
    return {
        "id": "manual-" + hashlib.sha1(key.encode()).hexdigest()[:10],
        "name": name,
        "artists": list(item.get("artists") or [name]),
        "date": day,
        "time": item.get("time"),
        "start_utc": None,
        "venue": item.get("venue"),
        "venue_id": None,
        "city": item.get("city", "Nashville" if area == "nashville" else None),
        "state": item.get("state", "TN" if area == "nashville" else None),
        "distance_mi": distance,
        "genre": item.get("genre"),
        "subgenre": None,
        "price": None,
        "status": item.get("status"),
        "onsale": None,
        "presales": [],
        "url": item.get("url"),
        "image": item.get("image"),
        "note": item.get("note"),
        "source": "manual",
        "_artist_ids": [],
        "_local": area == "nashville",
    }


def _dedupe(events: list[dict]) -> list[dict]:
    """Ticketmaster often lists one show several times (resale, packages); keep the best."""
    def rank(e):
        return (e["status"] == "canceled", e["price"] is None, len(e["name"]))

    best: dict[tuple, dict] = {}
    for e in events:
        head = norm(e["artists"][0] if e["artists"] else e["name"])
        key = (e["venue_id"] or norm(e["venue"]), e["date"], e["time"], head)
        if key not in best or rank(e) < rank(best[key]):
            best[key] = e
    kept = list(best.values())
    # Drop a manual entry once Ticketmaster lists the same artist on the same day.
    listed = {(e["date"], norm(a)) for e in kept if e["source"] != "manual" for a in e["artists"]}
    return [e for e in kept if e["source"] != "manual"
            or not any((e["date"], norm(a)) in listed for a in e["artists"])]


def _compact(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if v not in (None, "", [], False)}


def watch_entries(cfg: dict) -> list[dict]:
    """Watchlist items may be plain names or {"name", "aliases", "tm_id"} objects."""
    out = []
    for item in cfg.get("watchlist") or []:
        item = {"name": item} if isinstance(item, str) else dict(item)
        item["name"] = (item.get("name") or "").strip()
        if item["name"]:
            out.append(item)
    return out


def finalize(records, watch: list[dict], owner_of: dict[str, str], cfg: dict, today: date) -> list[dict]:
    """Tag watchlist matches, area and major venues; drop past/irrelevant shows; sort."""
    display = {}
    for artist in watch:
        for n in [artist["name"], *artist.get("aliases", [])]:
            if norm(n):
                display[norm(n)] = artist["name"]
    majors = [norm(v) for v in cfg["major_venues"] if norm(v)]
    out = []
    for rec in records:
        if rec["date"] < today.isoformat():
            continue
        hits = {owner_of[a] for a in rec.pop("_artist_ids") if a in owner_of}
        hits |= {display[norm(a)] for a in rec["artists"] if norm(a) in display}
        local = rec.pop("_local")
        near = rec["distance_mi"] is not None and rec["distance_mi"] <= cfg["radius_miles"]
        area = "nashville" if local or near else "nearby"
        if area == "nearby" and (not hits or (rec["distance_mi"] or 0) > cfg["nearby_radius_miles"]):
            continue  # outside Nashville only nearby shows of watchlist artists are tracked
        rec["area"] = area
        rec["watch"] = sorted(hits)
        venue = norm(rec["venue"])
        rec["major"] = area == "nashville" and any(m in venue for m in majors)
        out.append(rec)
    out = _dedupe(out)
    out.sort(key=lambda e: (e["date"], e["time"] or "99:99", e["name"].lower()))
    for rec in out:
        rec.pop("venue_id", None)
    return [_compact(r) for r in out]


def collect(client: Ticketmaster, cfg: dict, now: datetime) -> tuple[list[dict], list[dict]]:
    center = cfg["center"]
    point = geohash(center["lat"], center["lon"])
    start = now.replace(microsecond=0)
    end = start + timedelta(days=round(30.5 * cfg["months_ahead"]))
    exclude = [re.compile(r"\b" + re.escape(p.strip()) + r"\b", re.I)
               for p in cfg["exclude_name_patterns"] if p.strip()]
    found: dict[str, dict] = {}

    def add(raw_events, local: bool) -> None:
        for raw in raw_events:
            rec = build_event(raw, center, exclude, now)
            if rec is None:
                continue
            if rec["id"] in found:
                found[rec["id"]]["_local"] |= local
            else:
                rec["_local"] = local
                found[rec["id"]] = rec

    log(f"抓取 {center['name']} 周围 {cfg['radius_miles']} 英里内未来 {cfg['months_ahead']} 个月的音乐演出…")
    add(client.search_events({"classificationName": "music", "geoPoint": point,
                              "radius": cfg["radius_miles"], "unit": "miles"}, start, end), True)
    log(f"  {len(found)} 场")

    watch = watch_entries(cfg)
    owner_of: dict[str, str] = {}
    meta = []
    for artist in watch:
        ids = find_attraction_ids(client, artist)
        meta.append({"name": artist["name"], "found": bool(ids)})
        if not ids:
            log(f"  ! Ticketmaster 上没找到 “{artist['name']}”（可在 config.json 中为它设置 aliases 或 tm_id）")
            continue
        before = len(found)
        for aid in ids:
            owner_of[aid] = artist["name"]
            add(client.search_events({"attractionId": aid, "classificationName": "music",
                                      "geoPoint": point, "radius": cfg["nearby_radius_miles"],
                                      "unit": "miles"}, start, end), False)
        log(f"  关注 {artist['name']}: +{len(found) - before} 场（{cfg['nearby_radius_miles']} 英里内）")

    for item in cfg["manual_events"]:
        rec = build_manual_event(item, center, cfg["radius_miles"])
        if rec:
            found.setdefault(rec["id"], rec)

    today = now.astimezone(LOCAL_TZ).date()
    return finalize(found.values(), watch, owner_of, cfg, today), meta


# --------------------------------------------------------------------------- state & outputs


def update_state(events: list[dict], state: dict, today: str) -> list[dict]:
    """Stamp first_seen on every event; return watchlist shows not announced before."""
    seen = state.setdefault("seen", {})
    notified = state.setdefault("notified", {})
    state.setdefault("tracking_since", today)
    fresh = []
    for e in events:
        e["first_seen"] = seen.setdefault(e["id"], today)
        if e.get("watch") and e["id"] not in notified and e.get("status") != "canceled":
            notified[e["id"]] = today
            fresh.append(e)
    current = {e["id"] for e in events}
    cutoff = (date.fromisoformat(today) - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    for table in (seen, notified):
        for key in [k for k, v in table.items() if k not in current and v < cutoff]:
            del table[key]
    return fresh


def write_events_json(path: Path, payload: dict) -> None:
    """Pretty header, one event per line: compact yet gives readable git diffs."""
    head = [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}"
            for k, v in payload.items() if k != "events"]
    rows = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in payload["events"]]
    body = "[\n" + ",\n".join("    " + r for r in rows) + "\n  ]" if rows else "[]"
    path.write_text("{\n" + ",\n".join(head) + f',\n  "events": {body}\n}}\n', encoding="utf-8")


def _local_time(value: str | None) -> str | None:
    dt = parse_utc(value)
    return dt.astimezone(LOCAL_TZ).strftime("%m-%d %H:%M") if dt else None


def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _ics_fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets without splitting UTF-8 characters."""
    parts, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > 75:
            parts.append(cur)
            cur, size = " " + ch, 1 + n
        else:
            cur, size = cur + ch, size + n
    parts.append(cur)
    return "\r\n".join(parts)


def build_ics(events: list[dict]) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//nashville-concerts//watchlist//ZH",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Nashville 关注歌手演出",
             "X-WR-TIMEZONE:America/Chicago", "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
             "X-PUBLISHED-TTL:PT12H"]
    for e in events:
        if not e.get("watch"):
            continue
        stamp = e.get("first_seen", e["date"]).replace("-", "") + "T000000Z"
        lines += ["BEGIN:VEVENT", f"UID:{e['id']}@nashville-concerts", f"DTSTAMP:{stamp}"]
        start = parse_utc(e.get("start_utc"))
        if start:
            fmt = "%Y%m%dT%H%M%SZ"
            lines += [f"DTSTART:{start.astimezone(timezone.utc).strftime(fmt)}",
                      f"DTEND:{(start + timedelta(hours=3)).astimezone(timezone.utc).strftime(fmt)}"]
        elif e.get("time"):
            lines += [f"DTSTART;TZID=America/Chicago:{e['date'].replace('-', '')}T{e['time'].replace(':', '')}00",
                      "DURATION:PT3H"]
        else:
            lines.append(f"DTSTART;VALUE=DATE:{e['date'].replace('-', '')}")
        canceled = e.get("status") == "canceled"
        lines.append("SUMMARY:" + _ics_text(("[已取消] " if canceled else "") + e["name"]))
        where = ", ".join(x for x in (e.get("venue"), e.get("city"), e.get("state")) if x)
        if where:
            lines.append("LOCATION:" + _ics_text(where))
        desc = ["关注：" + "、".join(e["watch"])]
        if e.get("onsale"):
            desc.append(f"开售：{_local_time(e['onsale'])}（纳什维尔时间）")
        if e.get("url"):
            desc.append("购票：" + e["url"])
            lines.append("URL:" + e["url"])
        lines.append("DESCRIPTION:" + _ics_text("\n".join(desc)))
        if canceled:
            lines.append("STATUS:CANCELLED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


def _describe(e: dict) -> str:
    d = date.fromisoformat(e["date"])
    parts = [f"{e['date']}（周{WEEKDAYS[d.weekday()]}）" + (f" {e['time']}" if e.get("time") else "")]
    where = ", ".join(x for x in (e.get("venue"), e.get("city"), e.get("state")) if x)
    if where:
        parts.append(where + (f"（约 {round(e['distance_mi'])} 英里）" if e.get("area") == "nearby"
                              and e.get("distance_mi") is not None else ""))
    if e.get("onsale"):
        parts.append(f"开售 {_local_time(e['onsale'])}")
    for p in e.get("presales", [])[:1]:
        parts.append(f"预售 {_local_time(p['start'])}" + (f"（{p['name']}）" if p.get("name") else ""))
    if e.get("price"):
        parts.append(f"${e['price']['min']:g}–${e['price']['max']:g}")
    link = f" · [购票]({e['url']})" if e.get("url") else ""
    return f"- **{'、'.join(e['watch'])}** — {e['name']} · " + " · ".join(parts) + link


def build_notification(fresh: list[dict], bootstrap: bool, site_url: str | None) -> tuple[str, str]:
    names = sorted({n for e in fresh for n in e["watch"]})
    if bootstrap:
        title = f"🎤 开始追踪：关注歌手目前已公布 {len(fresh)} 场演出"
        intro = "首次抓取完成，以下是关注歌手目前已公布的演出。之后只有新增演出才会再开 Issue 提醒你。"
    else:
        title = "🎤 关注歌手新演出：" + "、".join(names[:4]) + (" 等" if len(names) > 4 else "")
        intro = "发现关注歌手的新演出："
    lines = [intro, ""]
    for area, heading in (("nashville", "纳什维尔"), ("nearby", "周边城市")):
        group = [e for e in fresh if e.get("area") == area]
        if group:
            lines += [f"### {heading}（{len(group)} 场）", ""] + [_describe(e) for e in group] + [""]
    if site_url:
        lines.append(f"网站：{site_url}")
    body = "\n".join(lines)
    if len(body) > 60000:  # GitHub issue bodies max out at 65536 chars
        body = body[:60000] + "\n\n…（内容过长已截断，完整列表见网站）"
    return title, body


def site_url_from_env() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io/"
    return f"https://{owner.lower()}.github.io/{name}/"


def set_outputs(**values: bool) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={'true' if v else 'false'}\n")


def load_config(path: Path) -> dict:
    cfg = {**DEFAULT_CONFIG, **load_json(path, {})}
    cfg["center"] = {**DEFAULT_CONFIG["center"], **(cfg.get("center") or {})}
    return cfg


# --------------------------------------------------------------------------- entry point


def main(argv=None, client: Ticketmaster | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=Path(__file__).resolve().parent.parent,
                        help="project root (default: this repository)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    data_dir, build_dir = root / "data", root / "build"
    for stale in ("notify.md", "notify_title.txt"):
        (build_dir / stale).unlink(missing_ok=True)

    cfg = load_config(root / "config.json")
    now = now or datetime.now(timezone.utc)
    if client is None:
        key = os.environ.get("TICKETMASTER_API_KEY", "").strip()
        if not key:
            log("::warning::未设置 TICKETMASTER_API_KEY，跳过抓取（网站继续显示现有数据）。")
            set_outputs(changed=False, notify=False)
            return 0
        client = Ticketmaster(key)

    try:
        events, watch_meta = collect(client, cfg, now)
    except ApiError as err:
        log(f"::error::{err}")
        return 1

    data_dir.mkdir(parents=True, exist_ok=True)
    state = load_json(data_dir / "state.json", {})
    bootstrap = not state.get("seen")
    today = now.astimezone(LOCAL_TZ).date().isoformat()
    fresh = update_state(events, state, today)

    payload = {
        "generated_at": iso_utc(now),
        "source": "Ticketmaster Discovery API",
        "center": cfg["center"]["name"],
        "radius_miles": cfg["radius_miles"],
        "nearby_radius_miles": cfg["nearby_radius_miles"],
        "tracking_since": state["tracking_since"],
        "watchlist": watch_meta,
        "events": events,
    }
    previous = load_json(data_dir / "events.json", {})
    changed = any(previous.get(k) != v for k, v in payload.items() if k != "generated_at")

    write_events_json(data_dir / "events.json", payload)
    (data_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (data_dir / "watchlist.ics").write_bytes(build_ics(events).encode("utf-8"))

    if fresh:
        title, body = build_notification(fresh, bootstrap, site_url_from_env())
        build_dir.mkdir(exist_ok=True)
        (build_dir / "notify_title.txt").write_text(title, encoding="utf-8")
        (build_dir / "notify.md").write_text(body, encoding="utf-8")
    set_outputs(changed=changed, notify=bool(fresh))

    watched = sum(1 for e in events if e.get("watch"))
    nearby = sum(1 for e in events if e.get("area") == "nearby")
    log(f"完成：{len(events)} 场演出（关注歌手 {watched} 场，其中周边城市 {nearby} 场），"
        f"API 请求 {client.calls} 次，新的关注演出 {len(fresh)} 场，数据{'有' if changed else '无'}变化。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
