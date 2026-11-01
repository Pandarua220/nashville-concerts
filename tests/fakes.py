"""In-memory stand-in for the Ticketmaster Discovery API, used by the tests.

Responses follow the shape documented at
https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/
"""

from __future__ import annotations

import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

VENUES = {
    "bridgestone": ("KovZpZAEAlaA", "Bridgestone Arena", "Nashville", "TN", 36.1592, -86.7785),
    "ryman": ("KovZpZAJ6nlA", "Ryman Auditorium", "Nashville", "TN", 36.1612, -86.7785),
    "basement": ("KovZpZA1vFkA", "The Basement East", "Nashville", "TN", 36.1810, -86.7486),
    "firstbank": ("KovZ917A_c7", "FirstBank Amphitheater", "Franklin", "TN", 35.9220, -86.8547),
    "atlanta": ("KovZpZAJ7elA", "State Farm Arena", "Atlanta", "GA", 33.7573, -84.3963),
    "london": ("KovZ9177Xn0", "Wembley Stadium", "London", "", 51.5560, -0.2796),
}


def tm_event(event_id, name, local_date, local_time="19:30", venue="bridgestone", attractions=(),
             genre="Pop", price=None, status="onsale", onsale=None, presales=(), time_tba=False):
    vid, vname, city, state, lat, lon = VENUES[venue]
    hour, minute = map(int, local_time.split(":"))
    # Nashville is UTC-6 in winter / UTC-5 in summer; tests only need a plausible instant.
    start_utc = datetime.fromisoformat(local_date).replace(hour=hour, minute=minute,
                                                           tzinfo=timezone.utc) + timedelta(hours=6)
    event = {
        "name": name,
        "type": "event",
        "id": event_id,
        "url": f"https://www.ticketmaster.com/event/{event_id}",
        "images": [
            {"ratio": "16_9", "url": f"https://img.example/{event_id}_205.jpg", "width": 205, "height": 115},
            {"ratio": "16_9", "url": f"https://img.example/{event_id}_640.jpg", "width": 640, "height": 360},
            {"ratio": "3_2", "url": f"https://img.example/{event_id}_3x2.jpg", "width": 1024, "height": 683},
        ],
        "sales": {"public": {"startDateTime": onsale or "2026-01-01T15:00:00Z", "startTBD": False},
                  "presales": [{"name": n, "startDateTime": s, "endDateTime": e} for n, s, e in presales]},
        "dates": {
            "start": {"localDate": local_date, "localTime": f"{local_time}:00",
                      "dateTime": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "dateTBD": False, "dateTBA": False, "timeTBA": time_tba, "noSpecificTime": False},
            "timezone": "America/Chicago",
            "status": {"code": status},
        },
        "classifications": [{"primary": True, "segment": {"name": "Music"}, "genre": {"name": genre},
                             "subGenre": {"name": "Undefined"}}],
        "_embedded": {
            "venues": [{"name": vname, "id": vid, "city": {"name": city}, "state": {"stateCode": state},
                        "location": {"latitude": str(lat), "longitude": str(lon)}}],
            "attractions": [{"name": n, "id": i} for i, n in attractions],
        },
    }
    if price:
        event["priceRanges"] = [{"type": "standard", "currency": "USD", "min": price[0], "max": price[1]}]
    return event


class FakeDiscoveryApi:
    """Callable fetch(url) -> dict that serves canned events like the real API would."""

    def __init__(self, local_events=(), artists=None, artist_events=None, fail_with=None):
        self.local_events = list(local_events)
        self.artists = artists or {}            # keyword -> [(id, name), ...]
        self.artist_events = artist_events or {}  # attraction id -> [event, ...]
        self.fail_with = fail_with
        self.requests: list[dict] = []

    def __call__(self, url: str) -> dict:
        parsed = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        self.requests.append({"path": parsed.path, **q})
        if self.fail_with:
            raise urllib.error.HTTPError(url, self.fail_with, "error", None, None)
        if parsed.path.endswith("/attractions.json"):
            found = self.artists.get(q["keyword"], [])
            return {"_embedded": {"attractions": [{"id": i, "name": n} for i, n in found]}} if found else {
                "page": {"totalElements": 0}}
        assert parsed.path.endswith("/events.json"), parsed.path
        size, page = int(q["size"]), int(q["page"])
        assert size * page < 1000, "deep paging beyond the API limit"
        pool = self.artist_events.get(q["attractionId"], []) if "attractionId" in q else self.local_events
        lo, hi = q["startDateTime"], q["endDateTime"]
        hits = [e for e in pool if lo <= e["dates"]["start"]["dateTime"] <= hi]
        total = len(hits)
        chunk = hits[page * size:(page + 1) * size]
        body = {"page": {"size": size, "totalElements": total,
                         "totalPages": (total + size - 1) // size, "number": page}}
        if chunk:
            body["_embedded"] = {"events": chunk}
        return body
