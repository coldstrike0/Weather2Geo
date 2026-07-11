# weather2geo

**Reverse weather OSINT tool.** Given a local time, temperature, and weather condition, find candidate locations whose current local hour, weather code, and temperature match a query.

Useful when a subject posts a photo or video with observable environmental cues — time of day visible in shadows or clocks, weather visible through windows, ambient temperature mentioned in captions — and you want to generate a shortlist of candidate locations to investigate further.

No API key required. Uses [Open-Meteo](https://open-meteo.com/) (free, no auth) and [GeoNames](https://www.geonames.org/) city data.

---

## Example

Someone posts at what appears to be early afternoon, partly cloudy, around 24°C. Running:

```
python weather2geo.py --hour 13 --condition "Partly cloudy" --temp 24 --cities-file cities15000.txt
``````

---

## Disclaimer

This tool produces **candidate locations**, not definitive identifications. Results are correlations based on model weather data and city population thresholds — they should be treated as investigative leads, not conclusions.

- Weather data from Open-Meteo is forecast/model-based and may not exactly reflect real-time surface conditions.
- GeoNames city data is provided as-is; small or rural localities may be absent entirely.
- Time matching is by hour only — a ±59 minute window is inherent to the approach.
- Multiple unrelated regions of the world will often share the same hour, temperature, and weather code simultaneously.

Use responsibly and in accordance with applicable laws. This tool is intended for legitimate OSINT research, journalism, and educational purposes.

---

## Consumer Weather Label → WMO Condition Cheatsheet

Most weather apps (Windows, iPhone, Google, AccuWeather) use plain-English labels that don't map 1:1 to WMO codes. Use this table to translate what you see on a widget into what to pass to `--condition`.

| What your app says | Use this with `--condition` |
|---|---|
| Sunny / Clear | `Clear sky` |
| Mostly sunny | `Mainly clear` |
| Partly sunny / Partly cloudy | `Partly cloudy` |
| Mostly cloudy / Cloudy | `Overcast` |
| Hazy / Foggy | `Fog` |
| Drizzle / Light drizzle | `Light drizzle` |
| Drizzle | `Moderate drizzle` |
| Light rain / Sprinkles | `Slight rain` |
| Rain / Rainy | `Moderate rain` |
| Heavy rain | `Heavy rain` |
| Freezing rain | `Light freezing rain` |
| Flurries / Light snow | `Slight snow` |
| Snow / Snowy | `Moderate snow` |
| Heavy snow / Blizzard | `Heavy snow` |
| Snow showers | `Slight snow showers` |
| Rain showers | `Slight rain showers` |
| Thunderstorm / Storms | `Thunderstorm` |
| Thunderstorm with hail | `Thunderstorm with slight hail` |

> When in doubt, run `python weather2geo.py --list` to see every valid condition name, and pick the closest match. If results come back empty, try an adjacent condition — the boundary between e.g. `Partly cloudy` and `Overcast` varies by data source.

---

## How it works

1. **Hour filter** — loads a GeoNames city database and filters for every city where the current local time matches the target hour. This eliminates ~23/24 of the world immediately.
2. **Weather fetch** — queries Open-Meteo in parallel batches for current conditions at every candidate city.
3. **Match + cluster** — filters by WMO weather code and temperature tolerance, then clusters nearby matches geographically (default 50 km radius).
4. **Major city annotation** — annotates each cluster with the nearest major city (pop ≥ 500k), measuring from the closest cluster member for accuracy.

---

## Installation

```bash
git clone https://github.com/coldstrike0/Weather2Geo
cd Weather2Geo
python -m venv venv && source venv/bin/activate 
pip install -r requirements.txt
```

### GeoNames city data (recommended)

The GeoNames dataset gives significantly better coverage than the fallback JSON and includes pre-embedded timezone data (faster, no coordinate lookup).

```bash
# cities15000.txt — population > 15,000 or capitals (~25k entries, good balance)
curl -L https://download.geonames.org/export/dump/cities15000.zip -o cities15000.zip
unzip cities15000.zip

# cities5000.txt — population > 5,000 (~50k entries, more coverage)
curl -L https://download.geonames.org/export/dump/cities5000.zip -o cities5000.zip
unzip cities5000.zip
```

Without `--cities-file`, the tool falls back to downloading a JSON city list (~10 MB) and caching it locally. GeoNames is recommended.

---

## Usage

```
python weather2geo.py --hour HOUR --condition "CONDITION" --temp TEMP [OPTIONS]
```

### Required

| Flag | Description |
|------|-------------|
| `--hour` / `-h` | Local hour to match (0–23) |
| `--condition` / `-c` | Weather condition string (see `--list`) |
| `--temp` / `-t` | Target temperature (Celsius by default, Fahrenheit if `--unit F`) |

### Optional

| Flag | Default | Description |
|------|---------|-------------|
| `--tolerance` | 2.0 | Temperature tolerance ±° in the unit given |
| `--unit` / `-u` | C | Temperature unit: `C` or `F` |
| `--humidity` | — | Optional: target relative humidity % — tightens matches |
| `--humidity-tol` | 10 | Humidity tolerance ±% |
| `--wind` | — | Optional: target wind speed in km/h — tightens matches |
| `--wind-tol` | 8 | Wind speed tolerance ±km/h |
| `--cloud` | — | Optional: target cloud cover % — tightens matches |
| `--cloud-tol` | 15 | Cloud cover tolerance ±% |
| `--cluster-km` | 50 | Clustering radius in km |
| `--workers` | 5 | Parallel HTTP workers (higher risks rate limiting — see note below) |
| `--cities-file` | *(JSON fallback)* | Path to GeoNames `.txt` or cached `.json` |
| `--list` / `-l` | — | Print all valid condition names and exit |

### List valid conditions

```bash
python weather2geo.py --list
```

### Improving precision with extra weather variables

Hour + temperature + condition alone can return a wide spread when a large weather system covers a region — many cities share the same headline condition. If your source also shows humidity, wind, or cloud cover (most phone/desktop weather widgets do), add them to dramatically narrow the candidates:

```bash
python weather2geo.py --hour 13 --condition "Partly cloudy" --temp 24 \
    --humidity 45 --wind 12 --cloud 40 --cities-file cities15000.txt
```

Each extra filter is optional and only applied when supplied. They're pulled from the same API call, so they cost nothing extra in requests.

### A note on workers and rate limiting

Open-Meteo's free tier rate-limits requests, and multi-location batches count toward that limit per location, not per request. Firing too many parallel workers bursts past the limit, and rate-limited batches were previously dropped silently — producing incomplete, non-deterministic coverage between runs. The tool now retries rate-limited batches with backoff and **warns you if any batch ultimately fails** so you know coverage is incomplete. The default of 5 workers is a safe balance; raise it only if you're not seeing rate-limit warnings.

### Fahrenheit input

```bash
python weather2geo.py --hour 13 --condition "Partly cloudy" --temp 75 --unit F --cities-file cities15000.txt
```

### Wider temperature tolerance

If you're working from an imprecise cue ("it looks warm, maybe mid-20s"):

```bash
python weather2geo.py --hour 13 --condition "Partly cloudy" --temp 24 --tolerance 4
```

---
## Limitations

- **Weather granularity** — Open-Meteo returns WMO weather codes. Conditions like "partly cloudy" vs "mainly clear" are distinct codes; if you're guessing from a photo, try adjacent conditions if you get no results.
- **City coverage** — GeoNames offers several tiers by minimum population: `cities15000.txt` (population > 15,000 or capitals, ~25k entries), `cities5000.txt` (population > 5,000, ~50k entries), `cities1000.txt` (~130k entries), and `cities500.txt` (~185k entries). Lower thresholds mean better rural and small-town coverage, but more cities pass the hour filter, which means more Open-Meteo batch requests and a slower run.
- **Batch drop rate** — Open-Meteo's batch endpoint occasionally returns fewer results than requested. This is a known upstream behaviour; re-running usually recovers dropped entries.
- **Time resolution** — matching is by hour, not minute. A photo posted at 13:55 and one at 13:05 are indistinguishable to this tool.
- **Temperature units** — input accepts Celsius (default) or Fahrenheit via `--unit F`. Output is always displayed in Celsius internally; the header shows your original unit.

---

## Credits

- Original project: [elliott-diy/Weather2Geo](https://github.com/elliott-diy/Weather2Geo)
- Weather data: [Open-Meteo](https://open-meteo.com/) — free, no API key
- City data: [GeoNames](https://www.geonames.org/) — CC BY 4.0
