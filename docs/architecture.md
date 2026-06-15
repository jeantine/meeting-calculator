# MeetingCalculator — Architecture Diagrams

## 1 · System Architecture

```mermaid
graph TD
    Browser["🌐 Browser\nindex.html"]
    Flask["Flask App\napp.py :5001"]
    CSV1["airports.csv\n~10 k airports"]
    CSV2["routes.csv\nair graph"]
    Rail["_RAIL_EDGES + RAIL_STATIONS\ninline in app.py\nrail graph"]
    Cities["CITIES dict\ninline in app.py\ncity→IATA/rail map"]
    SerpAPI["SerpAPI\nlive prices"]

    Browser -->|"POST /api/find_destinations"| Flask
    Browser -->|"POST /api/get_routes"| Flask
    Browser -->|"POST /api/get_live_prices"| Flask
    Browser -->|"GET /api/search_city"| Flask

    Flask -->|"loaded at startup"| CSV1
    Flask -->|"loaded at startup"| CSV2
    Flask -->|"built at startup (inline)"| Rail
    Flask -->|"built at startup (inline)"| Cities
    Flask -->|"on demand (optional)"| SerpAPI
```

## 2 · find_destinations Request Flow

```mermaid
sequenceDiagram
    participant B as Browser
    participant F as Flask
    participant D as find_meeting_destinations()
    participant DJ as Dijkstra (air)
    participant DR as Dijkstra (rail)
    participant SC as Scoring & Ranking

    B->>F: POST {attendees, nights, continent_filter}
    F->>D: validate & delegate
    D->>DJ: dijkstra_all() per unique origin airport
    D->>DR: dijkstra_rail_all() per unique rail station
    D->>SC: score each candidate city
    SC-->>D: city_scores dict
    D->>D: hops-first selection → top-N
    D->>D: re-sort by metric (cost/carbon/hops)
    D-->>F: (ranked, home, greenest)
    F-->>B: JSON {overall, home, greenest}
```

## 3 · Routing & Scoring Pipeline

```mermaid
flowchart LR
    subgraph Routing["Route Finding (per attendee→dest)"]
        direction TB
        Air["Air route\nfind_best_route()"]
        Rail["Rail route\nfind_best_rail_route()"]
        Hybrid["Hybrid\ntrain→hub→fly"]
        GW["Gateway\nfly→hub→train"]
        MixedSel["_MixedRouteChoice\nhops-first hub pick\n(short-rail exception)"]
        Best["pick lowest\nhops → dist"]
        Air --> Best
        Rail --> Best
        Hybrid --> MixedSel
        GW --> MixedSel
        MixedSel --> Best
    end

    subgraph Scoring["Fare & Carbon Estimation"]
        direction TB
        Fare["estimate_fare()\ndist-band base\n+ region factor\n+ hub discount"]
        RailFare["estimate_rail_fare()\ndist-band base\n+ transfer penalty"]
        Hotel["estimate_hotel_cost()\n+ estimate_hotel_carbon()"]
        Total["sum across\nall attendees"]
        Fare --> Total
        RailFare --> Total
        Hotel --> Total
    end

    subgraph Ranking["Ranking (find_meeting_destinations)"]
        direction TB
        HopsFirst["Sort by hops → build top-N"]
        RedisplayC["Re-sort: cost → carbon → hops"]
        RedisplayGr["Re-sort: carbon → cost → hops"]
        HopsFirst --> RedisplayC
        HopsFirst --> RedisplayGr
    end

    Best --> Scoring
    Scoring --> Ranking
```

### Hub selection for mixed modes (`_MixedRouteChoice`)

The two mixed modes are mirror images — **hybrid** is train→hub→fly (rail-only
origin) and **gateway** is fly→hub→train (air origin → rail destination) — but
they pick their hub with **one shared rule**, so they can't drift apart:

- the candidate with the **fewest total hops** wins, ties broken by price;
- **except** a candidate whose rail leg is short (≤ `_SHORT_RAIL_KM`, 300 km) may
  win on price alone.

In other words a higher-hop itinerary only wins when its train is a quick hop —
never because a long, under-priced rail leg looks cheap. Without this guard the
fare model's under-pricing of long international rail produced absurd routes
(e.g. Lausanne→Balıkesir riding rail all the way to Istanbul, or Beijing→Sheffield
flying to a marginally-cheaper Brussels then training across the Channel).

The rule lives in **one place** — the `_MixedRouteChoice` selector — and is reused
by all four call sites: the hybrid and gateway branches of
`get_routes_for_destination()` (the drilldown) **and** the `hybrid_cost_maps` /
`gateway_cost_maps` builders in `find_meeting_destinations()` (the ranking), so
the table totals always match the route drilldown.

### A city is its airports (both ends)

A multi-airport city is a **single destination spanning all its airports**, so
each attendee independently lands at whichever airport is best for them:

- **European rail cities** are grouped by the `CITIES` dict (Paris = CDG + ORY),
  with `IATA_TO_CITY` mapping each airport back to its city-code.
- **Non-CITIES cities** are grouped at startup by `(city, country)` into
  `AIRPORT_CITY_GROUP` / `AIRPORT_CITY_REPR` (built next to `MAIN_AIRPORTS`).
  Each city collapses to one **representative** airport — the best-connected one
  — which keys its ranking row. Helpers `city_airports()` / `city_repr()` expose
  the grouping; `_dest_airports_and_rail()` and `get_routes_for_destination()`
  use it so the destination side sees every airport, just as an origin attendee
  already carries all of theirs.

Without the non-CITIES grouping the two ends were asymmetric: an origin used all
its airports but a destination used only the single IATA passed — so
London→Bangkok routed to DMK via Madras (no London nonstop into DMK) while
Bangkok→London flew BKK nonstop. Grouping makes both pick BKK.

> Caveat: grouping keys on the airports CSV's `city` string, so distinct metro
> airports filed under different city names stay separate (e.g. Newark/EWR is
> not grouped with New York's JFK + LGA).
