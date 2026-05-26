# Cost and Carbon Estimation

This document explains how the Meeting Calculator estimates flight cost and CO₂ emissions for each candidate destination. All figures are one-way per person unless stated otherwise.

---

## Overview

Estimates are produced by `estimate_fare(total_dist_km, num_stops, origin_iata, dest_iata)` in `app.py`. The function returns a `(price_usd, carbon_kg)` tuple for a **one-way** trip. The caller doubles both figures for a return journey, then multiplies by the number of travellers to get a group total.

The estimate is used at two points:

| Where | Purpose |
|-------|---------|
| `/api/find_destinations` | Rank candidate cities before any search is triggered |
| `/api/get_routes` | Show a per-attendee cost breakdown in the route detail panel |

When the user clicks **Get live prices**, the `/api/get_live_prices` endpoint queries SerpAPI Google Flights for a real return fare and uses that instead. Carbon falls back to the estimate when SerpAPI does not return CO₂ data.

---

## Carbon estimation

### Model

Carbon is calculated as:

```
carbon_kg = total_dist_km × _carbon_factor(total_dist_km)
```

`_carbon_factor` returns a **kg CO₂ per passenger-kilometre** value that decreases with distance, reflecting two real-world effects:

1. Short-haul flights have a high fixed overhead (takeoff/landing) spread over fewer kilometres.
2. Long-haul routes use large widebodies at high load factors, which are more efficient per seat-km.

### Distance bands

| Distance (one-way) | Factor (kg CO₂/pax-km) | Typical aircraft |
|--------------------|------------------------|-----------------|
| ≤ 750 km | 0.170 | Regional turboprop / narrow-body |
| 751 – 2 000 km | 0.130 | Short-to-medium narrow-body |
| 2 001 – 5 000 km | 0.105 | Medium haul |
| 5 001 – 9 000 km | 0.095 | Long-haul widebody (~85 % load factor) |
| > 9 000 km | 0.085 | Ultra-long-haul widebody |

These figures are in the same range as the ICAO Carbon Emissions Calculator and Google Flights methodology. They deliberately exclude the Radiative Forcing Index (RFI) multiplier — including RFI would roughly double the figures and is not used by most mainstream tools (Google Flights, ICAO). The choice was made to keep estimates comparable to what travellers see elsewhere.

### Round-trip and group totals

```
carbon_kg_person_return = carbon_kg_oneway × 2
carbon_kg_group_return  = carbon_kg_person_return × attendee_count
```

### Live-price carbon path

When SerpAPI returns a `carbon_emissions.this_flight` value (in grams), that is used instead:

```
carbon_kg_person_return = (carbon_g_oneway × 2) / 1000
```

If SerpAPI returns `null` or omits the field, the distance-based estimate above is used as a fallback.

---

## Cost estimation

The fare model has three layers applied in sequence:

1. **Distance-band base fare** — captures the broad shape of airline economics
2. **Connection penalty** — extra cost per stop
3. **Route price factor** — adjusts for how competitive a specific market is

### 1. Distance-band base fare

| Distance (one-way) | Formula |
|--------------------|---------|
| ≤ 500 km | $60 + dist × $0.18 |
| 501 – 1 500 km | $80 + dist × $0.14 |
| 1 501 – 4 000 km | $130 + dist × $0.10 |
| 4 001 – 8 000 km | $250 + dist × $0.07 |
| 8 001 – 12 000 km | $350 + dist × $0.065 |
| > 12 000 km | $450 + dist × $0.06 |

The decreasing per-km rate reflects that widebody aircraft become more economical on longer sectors while base fares for short hops are dominated by airport fees and fixed crew costs.

### 2. Connection penalty

Each stop (change of plane) adds **$60** to the one-way estimate:

```
base_with_stops = base + num_stops × 60
```

This reflects the higher taxes, airport fees, and booking complexity of connecting itineraries, which typically cost more than a direct fare of the same total distance.

### 3. Route price factor

A multiplicative factor is applied to account for how competitive a given market is. It combines two independent adjustments:

```
price_factor = region_pair_multiplier × hub_factor
one_way_usd  = round((base_with_stops) × price_factor)
```

#### 3a. Region-pair multiplier

Different continent pairs have very different pricing dynamics, driven by the presence (or absence) of low-cost carriers, route density, and regulatory environment. The multipliers are applied symmetrically (London→Dubai == Dubai→London):

| Route type | Multiplier | Rationale |
|------------|-----------|-----------|
| Intra-Europe | **0.55** | Ryanair/EasyJet dominate; highest LCC penetration globally |
| Intra-North America | **0.70** | Dense domestic competition (Southwest, Spirit, etc.) |
| Intra-Asia | **0.85** | Strong SE Asia LCC market (AirAsia, IndiGo, etc.) |
| Intra-South America | **1.10** | Fewer competitors; high taxes in some markets |
| Intra-Oceania | **1.05** | Pacific island routes can be thin |
| Intra-Africa | **1.25** | Limited competition; many thin routes |
| Europe ↔ North America | **0.90** | Very competitive transatlantic market |
| Europe ↔ Asia | **0.90** | Gulf carriers + European airlines compete aggressively |
| Europe ↔ Africa | **1.05** | Moderate competition |
| Europe ↔ South America | **1.00** | Baseline |
| Europe ↔ Oceania | **1.00** | Baseline |
| North America ↔ Asia | **0.95** | Transpacific — competitive but long |
| North America ↔ South America | **1.10** | Thinner routes |
| North America ↔ Africa | **1.20** | Very few direct options |
| North America ↔ Oceania | **1.00** | Baseline |
| Asia ↔ Africa | **1.15** | Growing but still limited connectivity |
| Asia ↔ South America | **1.20** | Rare direct services |
| Asia ↔ Oceania | **1.00** | Baseline |
| Africa ↔ South America | **1.30** | Extremely thin; connections usually required |
| Africa ↔ Oceania | **1.20** | Very few services |
| South America ↔ Oceania | **1.10** | Limited connectivity |

Middle Eastern airports (UAE, Qatar, Saudi Arabia etc.) are classified as Asia for this purpose, so DXB↔LHR uses the **Europe ↔ Asia** multiplier of 0.90.

#### 3b. Hub competition discount

When **both** endpoints are in the set of ~50 major international hubs (see `_TOP_HUBS` in `app.py`), an additional **12% discount** is applied:

```
hub_factor = 0.88  (if both airports are top hubs)
hub_factor = 1.00  (otherwise)
```

Hub-to-hub routes attract the greatest number of carriers — for example LHR↔JFK is served by British Airways, Virgin, American, Delta, and others — which keeps prices lower than equivalent-distance thin routes.

### Example calculation

**Vienna (VIE) → London Heathrow (LHR), 1 stop, 1 245 km**

| Step | Calculation | Value |
|------|-------------|-------|
| Distance band | 501–1 500 km: $80 + 1245 × $0.14 | $254 |
| Connection penalty | 1 stop × $60 | +$60 |
| Subtotal | | $314 |
| Region-pair multiplier | Intra-Europe: × 0.55 | $173 |
| Hub discount | Both VIE and LHR are top hubs: × 0.88 | $152 |
| **One-way estimate** | | **$152** |
| **Return estimate (×2)** | | **$304** |

---

## How the estimates feed into the UI

### Destination ranking (`/api/find_destinations`)

For each candidate destination the app sums the return estimate across all attendee groups:

```
group_cost    = estimate_fare(dist, hops, origin, dest)[0] × 2 × group_size
group_carbon  = estimate_fare(dist, hops, origin, dest)[1] × 2 × group_size
total_cost    = Σ group_cost    (all attendee groups)
total_carbon  = Σ group_carbon  (all attendee groups)
```

Results are sorted by **lowest estimated cost** first, then **lowest total carbon** as the tie-breaker. Average flights is shown as an informational column but is not part of the sort. The same ordering applies to the Attendee Home Cities tab.

### Route detail panel (`/api/get_routes`)

The same `estimate_fare` call is repeated for each attendee group individually, using the specific best origin airport found for that group. This gives per-person and per-group estimates in the route breakdown.

### Live prices (`/api/get_live_prices`)

SerpAPI Google Flights is queried for a real return fare for the dates shown. If SerpAPI succeeds:

- **Price** — the live fare replaces the estimate entirely.
- **Carbon** — SerpAPI's own CO₂ figure is used when present; the distance estimate is the fallback.

If SerpAPI fails (network error, quota, unconfigured key), the distance estimate is used for both price and carbon and the source is flagged as `"estimate (SerpApi failed)"` in the response.
