# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Flask web app that finds the best city to hold a meeting for a group of attendees,
comparing **air and rail** options across Europe (and global air) by **cost** and
**carbon**. Backend is `app.py`; the frontend is a single file `index.html` with
inline JS/CSS. Live flight prices come optionally from SerpAPI.

> **Read `docs/architecture.md` first** — it has the system, request-flow, and
> scoring-pipeline diagrams. Keep it in sync with the code (see "Doc discipline").

## Running

```bash
python3 app.py            # dev server on http://localhost:5001
pytest                    # run the test suite (tests/test_meeting_finder.py)
```

- Deployment uses gunicorn (`Procfile`: `gunicorn app:app`).
- `SERPAPI_KEY` is read from `.env`; without it, live prices fall back to estimates.

## Data model (the non-obvious core)

Everything routing-related is built **in-memory at startup**. Two of the four data
sources are **inline Python structures**, not files:

| Concept | Source | Notes |
|---|---|---|
| Airports | `airports.csv` → `AIRPORTS` | via `load_airports()` |
| Airlines | `airlines.csv` → `AIRLINES` | name resolution for tooltips |
| Air graph | `routes.csv` → `GRAPH` | via `build_graph()`; directed |
| **Rail graph** | **`_RAIL_EDGES` + `RAIL_STATIONS`** (inline in `app.py`) | NOT a CSV |
| **City map** | **`CITIES` dict** (inline in `app.py`) | NOT a JSON file |

- **`CITIES` is the single source of truth.** `IATA_TO_CITY` and `STATION_TO_IATAS`
  are *derived* from it at startup — edit `CITIES`, not the derived dicts.
- **Station codes are 5 chars**: 2-char ISO country + 3-char city (e.g. `GBLON`,
  `ITROM`). Same key is used in `RAIL_STATIONS` and as the `'rail'` field in `CITIES`.
- The **air graph and rail graph are separate** (`GRAPH` vs `RAIL_GRAPH`) so air
  routing is untouched by rail changes. Routing combines them via hybrid/gateway legs.

## Editing the rail network (`_RAIL_EDGES`)

- Edges are `(station_a, station_b, distance_km, 'Operator')`.
- **Only add genuine direct (no-change) scheduled services.** Verify with a current
  timetable / web search before adding — do not infer a direct train from a map line.
- **List each pair once.** `RAIL_GRAPH` is built bidirectionally, so adding both
  directions creates a redundant duplicate. (Two such dupes already exist:
  `DEBER↔DEHAN`, `DEMUC↔ATSBG` — harmless, same weight.)
- `distance_km` is approximate great-circle/track km; the operator label is free text.
- The French network is intentionally **hub-and-spoke via Paris**, plus a curated set
  of verified TGV *intersecteurs* / Intercités that bypass Paris.

## Key functions & routes

- Routes: `/api/find_destinations` (POST), `/api/get_routes` (POST),
  `/api/get_live_prices` (POST), `/api/search_city` (GET).
- Core: `find_meeting_destinations()` orchestrates; `dijkstra_all()` /
  `dijkstra_rail_all()` do shortest-path; `find_best_route()` /
  `find_best_rail_route()` pick a leg; `estimate_fare()` / `estimate_rail_fare()` /
  `estimate_hotel_cost()` / `estimate_hotel_carbon()` do costing.
- Ranking is **hops-first** (fewest connections), then re-sorted by the chosen metric
  (cost / carbon / hops).

## Doc discipline

- **Update `docs/architecture.md` before pushing to `main`** when you change routing,
  scoring, data sources, or endpoints.
- Don't push directly to `main` without confirmation.

## Gotchas

- Stale/legacy files exist and should be ignored: `old_app.py`, `old_index.html`,
  `meeting_location.py.old` (and `.ESTIMATION.md.swp`).
- `index.html` is large and holds all UI logic inline — search within it rather than
  expecting separate JS/CSS files.
