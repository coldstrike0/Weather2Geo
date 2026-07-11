#!/usr/bin/env python3
"""
weather2geo — reverse weather OSINT tool
Given a local time + weather conditions, find all places on Earth currently matching.
Uses Open-Meteo (free, no API key) + GeoNames city data.
"""

import csv
import json
import os
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytz
import requests
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from timezonefinder import TimezoneFinder

app = typer.Typer(add_completion=False)
console = Console()

CITIES_URL = "https://raw.githubusercontent.com/lutangar/cities.json/master/cities.json"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_BATCH_SIZE = 100

MAJOR_CITY_MIN_POP = 500_000  # population threshold for "major city" anchor

# WMO Weather Interpretation Codes → human-readable condition
# https://open-meteo.com/en/docs#weathervariables
WMO_CONDITIONS = {
    0:  "Clear sky",
    1:  "Mainly clear",
    2:  "Partly cloudy",
    3:  "Overcast",
    45: "Fog",
    48: "Icy fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def cluster_locations(locations, max_distance_km=50):
    clusters = []
    for loc in locations:
        placed = False
        for cluster in clusters:
            if haversine(loc["lat"], loc["lon"], cluster[0]["lat"], cluster[0]["lon"]) <= max_distance_km:
                cluster.append(loc)
                placed = True
                break
        if not placed:
            clusters.append([loc])
    return sorted(clusters, key=len, reverse=True)


def nearest_major_city(cluster, major_cities, max_km=200):
    """
    Find the closest major city to the nearest cluster member.
    Measures from each cluster member individually and takes the minimum,
    so the distance reflects the closest actual match — not a centroid artifact.
    Returns a dict with name/country/distance_km, or None if nothing within max_km.
    """
    if not major_cities:
        return None

    best_city = None
    best_dist = float("inf")

    for city in major_cities:
        # Distance from this major city to its nearest cluster member
        min_member_dist = min(
            haversine(loc["lat"], loc["lon"], city["lat"], city["lon"])
            for loc in cluster
        )
        if min_member_dist < best_dist:
            best_dist = min_member_dist
            best_city = city

    if best_city and best_dist <= max_km:
        return {**best_city, "distance_km": round(best_dist)}
    return None


# ---------------------------------------------------------------------------
# City loading — two formats supported
# ---------------------------------------------------------------------------

def load_cities_geonames(filepath):
    """
    Load from a GeoNames TSV (cities500.txt / cities5000.txt / cities15000.txt).

    GeoNames column layout (tab-separated):
      0  geonameid   1  name        2  asciiname   3  alternatenames
      4  latitude    5  longitude   6  feature_class  7  feature_code
      8  country     9  cc2        10  admin1     11  admin2
     12  admin3     13  admin4     14  population 15  elevation
     16  dem        17  timezone   18  modification_date
    """
    cities = []
    skipped = 0
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if len(row) < 18:
                continue
            try:
                if row[6] != "P":          # populated places only
                    continue
                tz_name = row[17].strip()
                if not tz_name:
                    skipped += 1
                    continue
                cities.append({
                    "name":       row[1].strip(),
                    "country":    row[8].strip(),
                    "lat":        float(row[4].strip()),
                    "lon":        float(row[5].strip()),
                    "timezone":   tz_name,
                    "population": int(row[14]) if row[14].strip().lstrip("-").isdigit() else 0,
                })
            except Exception:
                continue
    if skipped:
        console.print(f"[dim]Skipped {skipped} rows with missing timezone.[/dim]")
    return cities, "geonames"


def load_cities_json(cache_path="cities_cache.json"):
    """Load lutangar JSON (download and cache on first run)."""
    if os.path.exists(cache_path):
        console.print(f"[dim]Loading cities from cache ({cache_path})...[/dim]")
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f), "json"

    console.print("[cyan]Downloading city database (~10 MB, cached after first run)...[/cyan]")
    r = requests.get(CITIES_URL, timeout=60)
    r.raise_for_status()
    cities = r.json()
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cities, f)
    console.print(f"[green]Cached {len(cities):,} cities → {cache_path}[/green]")
    return cities, "json"


def load_cities(cities_file=None):
    """
    Auto-detect and load the right dataset.
      --cities-file *.txt  → GeoNames TSV (includes timezone, no timezonefinder needed)
      --cities-file *.json → raw JSON
      (nothing)            → download/cache JSON fallback
    """
    if cities_file:
        if not os.path.exists(cities_file):
            console.print(f"[red]File not found:[/red] {cities_file}")
            raise typer.Exit(1)
        if cities_file.endswith(".txt"):
            console.print(f"[cyan]Loading GeoNames dataset: [bold]{cities_file}[/bold]...[/cyan]")
            cities, fmt = load_cities_geonames(cities_file)
            console.print(f"[green]Loaded {len(cities):,} populated places.[/green]")
            return cities, fmt
        else:
            console.print(f"[dim]Loading cities from {cities_file}...[/dim]")
            with open(cities_file, "r", encoding="utf-8") as f:
                return json.load(f), "json"
    return load_cities_json()


# ---------------------------------------------------------------------------
# Hour filtering — GeoNames uses embedded tz string; JSON needs coordinate lookup
# FIX: population is now carried through so we can use it for rep selection
# ---------------------------------------------------------------------------

def filter_by_hour(cities, fmt, target_hour, tf=None):
    matches = []
    for city in cities:
        try:
            lat = float(city["lat"])
            lon = float(city.get("lon") or city.get("lng"))

            if fmt == "geonames":
                tz_name = city["timezone"]
            else:
                tz_name = tf.timezone_at(lat=lat, lng=lon) if tf else None
                if not tz_name:
                    continue

            tz = pytz.timezone(tz_name)
            if datetime.now(tz).hour == target_hour:
                matches.append({
                    "name":       city["name"],
                    "country":    city["country"],
                    "lat":        lat,
                    "lon":        lon,
                    "timezone":   tz_name,
                    "population": city.get("population", 0),  # FIX: carry population
                })
        except Exception:
            continue
    return matches


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

# Weather variables pulled in every request (well under Open-Meteo's 10-variable
# single-call threshold, so this still counts as one call per location).
CURRENT_VARS = "temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m,cloud_cover,surface_pressure"


def fetch_weather_batch(batch, max_retries=4):
    """
    Query Open-Meteo for a batch of locations.

    Returns (results, status) where:
      results = list of (location_dict, weather_dict) tuples
      status  = "ok" | "failed"   (failed = exhausted retries / network error)

    Handles HTTP 429 (rate limit) with exponential backoff + jitter instead of
    silently dropping the batch. This is the fix for non-deterministic coverage:
    previously any non-200 (including rate limits) was swallowed as an empty result.
    """
    import random
    import time

    lats = ",".join(str(loc["lat"]) for loc in batch)
    lons = ",".join(str(loc["lon"]) for loc in batch)
    params = {
        "latitude":         lats,
        "longitude":        lons,
        "current":          CURRENT_VARS,
        "temperature_unit": "celsius",
        "wind_speed_unit":  "kmh",
        "forecast_days":    1,
    }

    for attempt in range(max_retries):
        try:
            r = requests.get(OPEN_METEO_URL, params=params, timeout=20)

            if r.status_code == 429:
                # Rate limited — back off and retry rather than dropping the batch
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else (2 ** attempt) + random.uniform(0, 1)
                time.sleep(wait)
                continue

            if r.status_code != 200:
                # Other server error — brief backoff then retry
                time.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
                continue

            data = r.json()
            if isinstance(data, dict):
                data = [data]
            results = []
            for i, loc_data in enumerate(data):
                if i >= len(batch):
                    break
                try:
                    current  = loc_data.get("current", {})
                    wmo_code = current.get("weather_code")
                    temp     = current.get("temperature_2m")
                    if wmo_code is None or temp is None:
                        continue
                    weather = {
                        "wmo_code":     wmo_code,
                        "temp":         float(temp),
                        "humidity":     current.get("relative_humidity_2m"),
                        "wind_speed":   current.get("wind_speed_10m"),
                        "cloud_cover":  current.get("cloud_cover"),
                        "pressure":     current.get("surface_pressure"),
                    }
                    results.append((batch[i], weather))
                except Exception:
                    continue
            return results, "ok"

        except requests.exceptions.RequestException:
            time.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
            continue

    return [], "failed"


def list_conditions():
    table = Table(title="Available Weather Conditions", box=box.SIMPLE)
    table.add_column("Code", style="dim")
    table.add_column("Condition", style="bold cyan")
    for code, desc in sorted(WMO_CONDITIONS.items()):
        table.add_row(str(code), desc)
    console.print(table)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@app.command()
def run(
    hour:        int   = typer.Option(None,  "--hour",       "-H", help="Local hour to match (0-23)"),
    condition:   str   = typer.Option(None,  "--condition",  "-c", help="Weather condition string (use --list for options)"),
    temp:        float = typer.Option(None,  "--temp",       "-t", help="Target temperature (Celsius by default, Fahrenheit if --unit F)"),
    unit:        str   = typer.Option("C",   "--unit",       "-u", help="Temperature unit: C (default) or F"),
    tolerance:   float = typer.Option(2.0,   "--tolerance",        help="Temperature tolerance ±° in the unit given (default: 2.0)"),
    humidity:    float = typer.Option(None,  "--humidity",         help="Optional: target relative humidity %% (tightens matches)"),
    humidity_tol: float = typer.Option(10.0, "--humidity-tol",     help="Humidity tolerance ±%% (default: 10)"),
    wind:        float = typer.Option(None,  "--wind",             help="Optional: target wind speed in km/h (tightens matches)"),
    wind_tol:    float = typer.Option(8.0,   "--wind-tol",         help="Wind speed tolerance ±km/h (default: 8)"),
    cloud:       float = typer.Option(None,  "--cloud",            help="Optional: target cloud cover %% (tightens matches)"),
    cloud_tol:   float = typer.Option(15.0,  "--cloud-tol",        help="Cloud cover tolerance ±%% (default: 15)"),
    cluster_km:  int   = typer.Option(50,    "--cluster-km",       help="Clustering radius in km (default: 50)"),
    workers:     int   = typer.Option(5,     "--workers",          help="Parallel HTTP workers (default: 5 — higher risks rate limiting)"),
    cities_file: str   = typer.Option(None,  "--cities-file",      help="Path to GeoNames .txt or JSON city file"),
    list_conds:  bool  = typer.Option(False, "--list",       "-l", help="List all weather conditions and exit"),
):
    """
    Reverse weather OSINT: find every city on Earth currently experiencing
    a specific hour, temperature, and weather condition.

    Basic usage:
        python weather2geo.py --hour 13 --condition "Partly cloudy" --temp 24

    With GeoNames for better city coverage (includes Calgary, major cities):
        python weather2geo.py --hour 13 --condition "Partly cloudy" --temp 24 \\
            --cities-file cities15000.txt

    Download GeoNames data:
        curl -L https://download.geonames.org/export/dump/cities15000.zip -o cities15000.zip
        unzip cities15000.zip
    """
    if list_conds:
        list_conditions()
        raise typer.Exit()

    if hour is None or condition is None or temp is None:
        console.print("[red]--hour, --condition, and --temp are all required.[/red]")
        console.print("[dim]Use --list to see valid condition names.[/dim]")
        raise typer.Exit(1)

    desired_condition = condition.strip()
    if desired_condition not in WMO_CONDITIONS.values():
        from difflib import get_close_matches
        suggestions = get_close_matches(desired_condition, WMO_CONDITIONS.values(), n=3, cutoff=0.5)
        console.print(f"[red]Unknown condition:[/red] [bold]{desired_condition}[/bold]")
        if suggestions:
            console.print("[yellow]Did you mean one of these?[/yellow]")
            for s in suggestions:
                console.print(f"  [cyan]{s}[/cyan]")
        else:
            console.print("[yellow]Run with --list to see all valid condition names.[/yellow]")
        raise typer.Exit(1)

    if not (0 <= hour <= 23):
        console.print("[red]Hour must be 0–23.[/red]")
        raise typer.Exit(1)

    unit = unit.upper()
    if unit not in ("C", "F"):
        console.print("[red]--unit must be C or F.[/red]")
        raise typer.Exit(1)

    # Convert F → C for all internal logic; display original input in header
    temp_display = f"{temp}°{'F' if unit == 'F' else 'C'}"
    tol_display  = f"±{tolerance}°{'F' if unit == 'F' else 'C'}"
    if unit == "F":
        temp      = (temp - 32) * 5 / 9
        tolerance = tolerance * 5 / 9

    desired_wmo = next(k for k, v in WMO_CONDITIONS.items() if v == desired_condition)

    console.print(Panel.fit(
        f"[bold]weather2geo[/bold] — Reverse Weather OSINT\n"
        f"Searching for: [cyan]{hour:02d}xx local[/cyan] | "
        f"[green]{desired_condition}[/green] | "
        f"[yellow]{temp_display} {tol_display}[/yellow]",
        style="bold blue"
    ))

    # ── Step 1: Load cities ──────────────────────────────────────────────────
    cities, fmt = load_cities(cities_file)
    source_label = "GeoNames" if fmt == "geonames" else "JSON cache"
    console.print(f"[dim]Loaded {len(cities):,} cities from {source_label}.[/dim]")

    # Build major city index for cluster annotation (GeoNames only — JSON lacks population)
    major_cities = []
    if fmt == "geonames":
        major_cities = [
            c for c in cities if c.get("population", 0) >= MAJOR_CITY_MIN_POP
        ]
        console.print(f"[dim]Indexed {len(major_cities):,} major cities (pop ≥ {MAJOR_CITY_MIN_POP:,}) for cluster annotation.[/dim]")

    # ── Step 2: Filter by local hour ─────────────────────────────────────────
    console.print(f"\n[cyan]Step 1/3:[/cyan] Finding cities where local time is [bold]{hour:02d}xx[/bold]...")

    tf = None
    if fmt == "json":
        console.print("[dim]  (JSON mode — deriving timezones from coordinates, this takes ~30s)[/dim]")
        tf = TimezoneFinder()

    candidates = filter_by_hour(cities, fmt, hour, tf)
    console.print(f"[green]→ {len(candidates):,} candidate cities match the hour.[/green]")

    if not candidates:
        console.print("[yellow]No cities found for that hour.[/yellow]")
        raise typer.Exit()

    # ── Step 3: Fetch weather ────────────────────────────────────────────────
    console.print(f"\n[cyan]Step 2/3:[/cyan] Fetching weather for all candidates (batches of {OPEN_METEO_BATCH_SIZE})...")
    batches = [candidates[i:i + OPEN_METEO_BATCH_SIZE] for i in range(0, len(candidates), OPEN_METEO_BATCH_SIZE)]

    all_weather = []
    completed = 0
    failed_batches = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_weather_batch, b): b for b in batches}
        for future in as_completed(futures):
            results, status = future.result()
            if status == "failed":
                failed_batches += 1
            all_weather.extend(results)
            completed += 1
            console.print(
                f"  [dim]Batch {completed}/{len(batches)} done — {len(all_weather)} results so far[/dim]",
                end="\r"
            )

    console.print(f"\n[green]→ Weather fetched for {len(all_weather):,} of {len(candidates):,} candidates.[/green]")
    if failed_batches:
        dropped = failed_batches * OPEN_METEO_BATCH_SIZE
        console.print(
            f"[yellow]⚠ {failed_batches} batch(es) failed after retries (~{dropped} cities not checked). "
            f"Coverage is INCOMPLETE — lower --workers or re-run to recover them.[/yellow]"
        )

    # ── Step 4: Match ────────────────────────────────────────────────────────
    active_filters = [f"condition=[bold]{desired_condition}[/bold]", f"temp=[bold]{temp_display} {tol_display}[/bold]"]
    if humidity is not None:
        active_filters.append(f"humidity=[bold]{humidity}% ±{humidity_tol}[/bold]")
    if wind is not None:
        active_filters.append(f"wind=[bold]{wind}km/h ±{wind_tol}[/bold]")
    if cloud is not None:
        active_filters.append(f"cloud=[bold]{cloud}% ±{cloud_tol}[/bold]")
    console.print(f"\n[cyan]Step 3/3:[/cyan] Matching " + ", ".join(active_filters) + "...")

    matches = []
    for loc, weather in all_weather:
        if weather["wmo_code"] != desired_wmo:
            continue
        if abs(weather["temp"] - temp) > tolerance:
            continue
        # Optional extra filters — only applied when the user supplies them and
        # the API returned a value for that variable.
        if humidity is not None and weather["humidity"] is not None:
            if abs(weather["humidity"] - humidity) > humidity_tol:
                continue
        if wind is not None and weather["wind_speed"] is not None:
            if abs(weather["wind_speed"] - wind) > wind_tol:
                continue
        if cloud is not None and weather["cloud_cover"] is not None:
            if abs(weather["cloud_cover"] - cloud) > cloud_tol:
                continue

        loc["wmo_code"]    = weather["wmo_code"]
        loc["temp"]        = weather["temp"]
        loc["condition"]   = WMO_CONDITIONS[weather["wmo_code"]]
        loc["humidity"]    = weather["humidity"]
        loc["wind_speed"]  = weather["wind_speed"]
        loc["cloud_cover"] = weather["cloud_cover"]
        matches.append(loc)

    console.print(f"[green]→ {len(matches)} matching locations found.[/green]")

    if not matches:
        console.print("\n[yellow]No matches. Try --tolerance or check --list for exact names.[/yellow]")
        raise typer.Exit()

    # ── Step 5: Cluster and display ──────────────────────────────────────────
    clusters = cluster_locations(matches, max_distance_km=cluster_km)

    console.print(Panel.fit(
        f"[bold green]Results: {len(matches)} locations across {len(clusters)} cluster{'s' if len(clusters) != 1 else ''}[/bold green]",
        style="green"
    ))

    for i, cluster in enumerate(clusters, 1):
        # FIX: pick the highest-population city as cluster representative
        rep = max(cluster, key=lambda x: x.get("population", 0))

        # FIX: annotate with nearest major city if rep itself isn't one
        anchor_label = f"[bold]{rep['name']}, {rep['country']}[/bold]"
        if major_cities and rep.get("population", 0) < MAJOR_CITY_MIN_POP:
            anchor = nearest_major_city(cluster, major_cities, max_km=200)
            if anchor and anchor["name"] != rep["name"]:
                anchor_label = (
                    f"[bold]{anchor['name']}, {anchor['country']}[/bold] "
                    f"[dim](~{anchor['distance_km']} km away — "
                    f"nearest major city; rep: {rep['name']})[/dim]"
                )

        console.print(
            f"\n[bold magenta]Cluster {i}[/bold magenta] "
            f"[dim]({len(cluster)} location{'s' if len(cluster) > 1 else ''})[/dim] "
            f"— near {anchor_label}"
        )

        # Sort cluster members by population descending so bigger cities list first
        for loc in sorted(cluster, key=lambda x: x.get("population", 0), reverse=True):
            pop_str = f"pop {loc['population']:,}" if loc.get("population") else "pop unknown"
            extra = []
            if loc.get("humidity") is not None:
                extra.append(f"{loc['humidity']:.0f}%RH")
            if loc.get("wind_speed") is not None:
                extra.append(f"{loc['wind_speed']:.0f}km/h")
            if loc.get("cloud_cover") is not None:
                extra.append(f"{loc['cloud_cover']:.0f}%cloud")
            extra_str = ("  " + " ".join(extra)) if extra else ""
            console.print(
                f"  [dim]·[/dim] [bold]{loc['country']}[/bold] {loc['name']:30s} "
                f"[yellow]{loc['temp']:+.1f}°C[/yellow]  "
                f"[green]{loc['condition']}[/green]  "
                f"[dim]{loc['lat']:.3f}, {loc['lon']:.3f}  {pop_str}{extra_str}[/dim]"
            )


if __name__ == "__main__":
    app()
