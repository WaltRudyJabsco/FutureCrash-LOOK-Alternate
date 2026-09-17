"""Information edges: a short list of named, canonical sources with provenance.

Every result carries the same receipt fields:
    edge · source · confidence (DIRECT | DERIVED | SEARCHED) · as_of · retrieved_at · source_url
Confidence describes provenance, not probability. There are no percentages.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from .. import __version__
from ..kernel.ollama import OllamaError, web_search as ollama_web_search
from ..settings import secret
from ..kernel.results import Result
from ..kernel.tools import NET, Context, Param, registry
from .host import resource_kind

UA = f"LOOK/{__version__} (local terminal assistant)"

EDGES = [
    ("WEATHER", "Open-Meteo", "live weather and forecast"),
    ("PLACE", "Open-Meteo geocoding", "place names → coordinates and timezone"),
    ("WIKI", "Wikipedia", "stable readable background"),
    ("DATA", "Wikidata", "structured entities and identifiers"),
    ("PAPERS", "Crossref", "DOI and bibliographic metadata"),
    ("ARCHIVE", "Internet Archive", "historical and primary artifacts"),
    ("WEB", "Ollama web search", "current, open-ended information (needs a key)"),
]

WEATHER_CODES = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog",
                 51: "light drizzle", 53: "drizzle", 55: "dense drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
                 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
                 81: "heavy showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
                 95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail"}


def http_json(url: str, timeout: float = 12.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def envelope(edge: str, source: str, source_url: str, payload: Dict[str, Any], confidence: str = "DIRECT",
             as_of: Any = None) -> Dict[str, Any]:
    return {"edge": edge, "source": source, "confidence": confidence, "as_of": as_of,
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source_url": source_url, **payload}


def _edge_result(label: str, env: Dict[str, Any], summary: str) -> Result:
    return Result.done(f"{label} · {env['source']} · {env['confidence']} · {summary}",
                       json.dumps(env, ensure_ascii=False), edge=env["edge"], source=env["source"])


def _guard(fn):
    def wrapped(ctx, **kwargs):
        try:
            return fn(ctx, **kwargs)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            return Result.failed(f"{fn.__name__.replace('_', ' ')} unavailable: {getattr(exc, 'reason', exc)}")
        except OllamaError as exc:
            return Result.failed(str(exc))
    wrapped.__name__ = fn.__name__
    return wrapped


def geocode(query: str, count: int = 5) -> List[Dict[str, Any]]:
    data = http_json("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": query, "count": max(1, min(count, 5)), "language": "en", "format": "json"}))
    return [{"name": r.get("name"), "region": ", ".join(x for x in (r.get("admin1"), r.get("country")) if x),
             "latitude": r.get("latitude"), "longitude": r.get("longitude"), "timezone": r.get("timezone")}
            for r in (data.get("results") or [])[:5]]


@registry.tool("place_lookup", "Resolve a place name or postal code to coordinates and timezone (Open-Meteo).", NET,
               label="place", params={"query": Param("string", "Place name")})
@_guard
def place_lookup(ctx: Context, query: str) -> Result:
    rows = geocode(query)
    env = envelope("PLACE", "Open-Meteo geocoding", "https://open-meteo.com/", {"query": query, "results": rows})
    first = f"{rows[0]['name']}, {rows[0]['region']}" if rows else "no match"
    return _edge_result("place", env, first)


@registry.tool("weather", "Current conditions and a 3-day forecast. ALWAYS use for weather questions.", NET,
               label="weather", params={"location": Param("string", "Place name"),
                                        "units": Param("string", "fahrenheit or celsius", required=False,
                                                       enum=["fahrenheit", "celsius"])})
@_guard
def weather(ctx: Context, location: str, units: str = "fahrenheit") -> Result:
    places = geocode(location, 1)
    if not places:
        return Result.done(f"weather · no place matches {location!r}", matches=0)
    place = places[0]
    metric = units == "celsius"
    params = {"latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
              "temperature_unit": "celsius" if metric else "fahrenheit", "wind_speed_unit": "kmh" if metric else "mph",
              "precipitation_unit": "mm" if metric else "inch",
              "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,cloud_cover,wind_speed_10m",
              "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
              "forecast_days": 3}
    data = http_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params))
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    days = []
    for i, day in enumerate((daily.get("time") or [])[:3]):
        def at(k):
            v = daily.get(k) or []
            return v[i] if i < len(v) else None
        days.append({"date": day, "sky": WEATHER_CODES.get(at("weather_code"), at("weather_code")),
                     "high": at("temperature_2m_max"), "low": at("temperature_2m_min"),
                     "precip_chance_pct": at("precipitation_probability_max"), "sunrise": at("sunrise"), "sunset": at("sunset")})
    cur["sky"] = WEATHER_CODES.get(cur.get("weather_code"), cur.get("weather_code"))
    env = envelope("WEATHER", "Open-Meteo", "https://open-meteo.com/",
                   {"place": place, "current": cur, "units": data.get("current_units") or {}, "forecast": days},
                   as_of=cur.get("time"))
    unit = "°C" if metric else "°F"
    summary = f"{place['name']}: {cur.get('temperature_2m')}{unit}, {cur.get('sky')}"
    return _edge_result("weather", env, summary)


@registry.tool("wikipedia", "Search English Wikipedia for stable background on a topic.", NET, label="wiki",
               params={"query": Param("string", "Topic")})
@_guard
def wikipedia(ctx: Context, query: str) -> Result:
    data = http_json("https://en.wikipedia.org/w/rest.php/v1/search/page?" + urllib.parse.urlencode({"q": query, "limit": 3}))
    rows = []
    for page in (data.get("pages") or [])[:3]:
        title = page.get("title") or ""
        rows.append({"title": title, "description": page.get("description"),
                     "excerpt": re.sub(r"<[^>]+>", "", page.get("excerpt") or ""),
                     "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="()_,-")})
    env = envelope("WIKI", "Wikipedia", "https://en.wikipedia.org/", {"query": query, "results": rows})
    return _edge_result("wiki", env, rows[0]["title"] if rows else "no article")


@registry.tool("wikidata", "Look up structured entities and identifiers on Wikidata.", NET, label="data",
               params={"query": Param("string", "Entity name")})
@_guard
def wikidata(ctx: Context, query: str) -> Result:
    data = http_json("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "wbsearchentities", "search": query, "language": "en", "format": "json", "limit": 5}))
    rows = [{"id": r.get("id"), "label": r.get("label"), "description": r.get("description"),
             "url": f"https://www.wikidata.org/wiki/{r.get('id')}"} for r in (data.get("search") or [])[:5]]
    env = envelope("DATA", "Wikidata", "https://www.wikidata.org/", {"query": query, "results": rows})
    return _edge_result("data", env, f"{rows[0]['label']} ({rows[0]['id']})" if rows else "no entity")


@registry.tool("papers", "Find scholarly works and DOIs via Crossref.", NET, label="papers",
               params={"query": Param("string", "Title, author, or topic")})
@_guard
def papers(ctx: Context, query: str) -> Result:
    data = http_json("https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": query, "rows": 5}))
    rows = []
    for it in ((data.get("message") or {}).get("items") or [])[:5]:
        parts = ((it.get("published-print") or it.get("published-online") or it.get("issued") or {}).get("date-parts") or [[]])[0]
        doi = it.get("DOI")
        rows.append({"title": (it.get("title") or [""])[0],
                     "authors": [" ".join(x for x in (a.get("given"), a.get("family")) if x) for a in (it.get("author") or [])[:6]],
                     "published": "-".join(map(str, parts)) if parts else None,
                     "container": (it.get("container-title") or [None])[0], "doi": doi,
                     "url": f"https://doi.org/{doi}" if doi else it.get("URL")})
    env = envelope("PAPERS", "Crossref", "https://api.crossref.org/", {"query": query, "results": rows})
    return _edge_result("papers", env, f"{len(rows)} works")


@registry.tool("archive", "Search the Internet Archive for historical objects, books, audio, film.", NET, label="archive",
               params={"query": Param("string", "What to look for")})
@_guard
def archive(ctx: Context, query: str) -> Result:
    params = {"q": query, "fl[]": ["identifier", "title", "creator", "date", "mediatype"], "rows": 5, "page": 1, "output": "json"}
    data = http_json("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params, doseq=True))
    rows = [{"identifier": d.get("identifier"), "title": d.get("title"), "creator": d.get("creator"), "date": d.get("date"),
             "mediatype": d.get("mediatype"), "url": f"https://archive.org/details/{d.get('identifier')}"}
            for d in ((data.get("response") or {}).get("docs") or [])[:5]]
    env = envelope("ARCHIVE", "Internet Archive", "https://archive.org/", {"query": query, "results": rows})
    return _edge_result("archive", env, f"{len(rows)} items")


@registry.tool("web_search", "General web search for current or open-ended information when no canonical edge fits.", NET,
               label="search", params={"query": Param("string", "Search query")},
               available=lambda: bool(secret("OLLAMA_API_KEY")))
@_guard
def web_search(ctx: Context, query: str) -> Result:
    data = ollama_web_search(query)
    rows = []
    for r in (data.get("results") or [])[:6]:
        url = r.get("url") or ""
        rows.append({"title": r.get("title"), "url": url, "kind": resource_kind(url) if url else "page",
                     "content": str(r.get("content") or "")[:1200]})
    env = envelope("WEB", "Ollama web search", "https://ollama.com/", {"query": query, "results": rows}, confidence="SEARCHED")
    return _edge_result("search", env, f"{len(rows)} results for {query!r}")
