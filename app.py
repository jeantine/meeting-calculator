"""
Meeting Location Finder — Flask API backend
Run with: python app.py
Then open: http://localhost:5000
"""

import math
import heapq
import logging
import urllib.request
import urllib.parse
import urllib.error
import json
from collections import defaultdict
from flask import Flask, jsonify, request, send_from_directory

# Set up logging — output goes to the terminal running app.py
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("meeting_finder")

import os

# Load .env file from the same directory as app.py (no extra dependencies needed)
_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "YOUR_SERPAPI_KEY_HERE")

# ---------------------------------------------------------------------------
# Bootstrap: load data once at startup
# ---------------------------------------------------------------------------
import csv, os

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

# ---------------------------------------------------------------------------
# Continent detection from lat/lon
# Bounding boxes ordered from most-specific to least to handle overlaps
# (e.g. Russia spans both Europe and Asia — we use lon to split)
# ---------------------------------------------------------------------------
def get_continent(lat, lon):
    """
    Returns one of: Africa, Antarctica, Asia, Europe,
                    North America, Oceania, South America
    Uses a polygon-free bounding-box approach — accurate for airport use.

    Check order matters: Middle East and Europe are tested before the broad
    Africa box so that Gulf-state airports (UAE, Saudi Arabia, Kuwait …) and
    southern-European airports (Greece, Malta, Cyprus …) are not swallowed
    by the Africa latitude band.
    """
    # Antarctica
    if lat < -60:
        return "Antarctica"

    # Oceania (Australia, NZ, Pacific islands)
    if -50 < lat < 10 and 110 < lon < 180:
        return "Oceania"
    if -50 < lat < -10 and 160 < lon <= 180:
        return "Oceania"

    # South America
    if -60 < lat < 15 and -82 < lon < -34:
        return "South America"

    # North America (including Caribbean and Central America)
    if 5 < lat < 85 and -168 < lon < -52:
        return "North America"
    # Greenland
    if lat > 55 and -75 < lon < -10:
        return "North America"

    # Middle East / Arabian Peninsula — must come before Africa so that Gulf
    # airports (DXB, DOH, RUH …) aren't swallowed by the Africa lat/lon box.
    if 12 < lat < 38 and 32 < lon < 65:
        return "Asia"

    # Europe — must come before Africa so that Mediterranean European airports
    # (ATH, LCA, MLA …) at ~35-38 °N aren't caught by the Africa box.
    if 35 < lat < 72 and -25 < lon < 65:
        return "Europe"

    # Africa
    if -40 < lat < 38 and -20 < lon < 55:
        return "Africa"

    # Asia (everything east of Europe / Urals that isn't already matched)
    if -10 < lat < 80 and 25 < lon <= 180:
        return "Asia"
    if 40 < lat < 82 and 65 < lon <= 180:
        return "Asia"

    return "Unknown"


def same_continent(iata_a, iata_b, airports):
    """Return True if both airports are on the same continent."""
    a = airports.get(iata_a, {})
    b = airports.get(iata_b, {})
    if not a or not b:
        return False
    return get_continent(a['lat'], a['lon']) == get_continent(b['lat'], b['lon'])


def load_airports(filepath):
    airports = {}
    with open(filepath, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        for row in reader:
            iata = row.get('IATA', '').strip()
            try:
                lat = float(row['Latitude'].strip())
                lon = float(row['Longitude'].strip())
            except (ValueError, KeyError):
                continue
            if iata and iata != '\\N':
                airports[iata] = {
                    'name':      row.get('Name', '').strip(),
                    'city':      row.get('City', '').strip(),
                    'country':   row.get('Country', '').strip(),
                    'lat':       lat,
                    'lon':       lon,
                    'continent': get_continent(lat, lon),
                }
    return airports

def build_graph(routes_filepath, airports):
    best_edge = {}
    with open(routes_filepath, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        for row in reader:
            src     = row.get('Source airport', '').strip()
            dst     = row.get('Destination airport', '').strip()
            airline = row.get('Airline', '').strip()
            if src not in airports or dst not in airports:
                continue
            dist = haversine(airports[src]['lat'], airports[src]['lon'],
                             airports[dst]['lat'], airports[dst]['lon'])
            key = (src, dst)
            if key not in best_edge or dist < best_edge[key][0]:
                best_edge[key] = (dist, airline)
    graph = defaultdict(list)
    for (src, dst), (dist, airline) in best_edge.items():
        graph[src].append((dst, dist, airline))
    return graph

_HERE = os.path.dirname(os.path.abspath(__file__))
AIRPORTS_FILE  = os.path.join(_HERE, 'airports.csv')
ROUTES_FILE    = os.path.join(_HERE, 'routes.csv')
AIRLINES_FILE  = os.path.join(_HERE, 'airlines.csv')
for p in [AIRPORTS_FILE, ROUTES_FILE]:
    if not os.path.exists(p):
        raise FileNotFoundError(f"Missing: {p} — place it alongside app.py")

print("Loading airports...")
AIRPORTS = load_airports(AIRPORTS_FILE)
print(f"  {len(AIRPORTS)} airports loaded.")
print("Building route graph...")
GRAPH = build_graph(ROUTES_FILE, AIRPORTS)
print(f"  Graph ready.")

# Find the largest weakly-connected component so isolated island airports
# (e.g. DUT/Unalaska, which only connects to 3 local strips with no onward routes)
# are excluded from city search results.
_undirected = {}
for _src, _neighbors in GRAPH.items():
    for (_dst, _dist, _al) in _neighbors:
        _undirected.setdefault(_src, set()).add(_dst)
        _undirected.setdefault(_dst, set()).add(_src)

_visited = set()
_components = []
for _start in _undirected:
    if _start in _visited:
        continue
    _comp = set()
    _stack = [_start]
    while _stack:
        _node = _stack.pop()
        if _node in _visited:
            continue
        _visited.add(_node)
        _comp.add(_node)
        _stack.extend(_undirected.get(_node, set()) - _visited)
    _components.append(_comp)

MAIN_AIRPORTS = max(_components, key=len) if _components else set()
print(f"  Main network: {len(MAIN_AIRPORTS)} airports ({len(GRAPH) - len(MAIN_AIRPORTS)} stranded).")

AIRLINES = {}
if os.path.exists(AIRLINES_FILE):
    with open(AIRLINES_FILE, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        for row in reader:
            iata = row.get('IATA', '').strip()
            name = row.get('Name', '').strip()
            if iata and iata != '\\N' and name:
                AIRLINES[iata] = name
    print(f"  {len(AIRLINES)} airlines loaded.")

# ---------------------------------------------------------------------------
# Core algorithms
# ---------------------------------------------------------------------------

def find_airports_by_city(query):
    q = query.strip().lower()
    matches = []
    for iata, info in AIRPORTS.items():
        if q in info['city'].lower() or q in info['country'].lower() or q == iata.lower():
            matches.append((iata, info))
    matches.sort(key=lambda x: (0 if x[1]['city'].lower() == q else 1,
                                 x[1]['city'], x[1]['name']))
    # Group by city+country, skipping airports with no city name
    groups = {}
    for iata, info in matches:
        if not info['city'].strip():
            continue
        key = f"{info['city']}, {info['country']}"
        groups.setdefault(key, []).append(iata)
    # Only return cities where at least one airport has actual routes
    results = []
    for loc, iatas in groups.items():
        routable = [i for i in iatas if i in GRAPH and i in MAIN_AIRPORTS]
        if not routable:
            continue
        results.append({
            'location': loc,
            'iatas': routable,
            'continent': AIRPORTS[routable[0]]['continent'],
            'airports': [{'iata': i, 'name': AIRPORTS[i]['name'],
                          'continent': AIRPORTS[i]['continent']} for i in routable]
        })
        if len(results) == 20:
            break
    return results


def find_best_route(origin, destination):
    """Fewest hops first, then shortest distance. Returns path or None."""
    heap = [(0, 0.0, origin, [])]
    visited = {}
    while heap:
        hops, total_dist, current, path = heapq.heappop(heap)
        if current in visited:
            ph, pd = visited[current]
            if hops > ph or (hops == ph and total_dist >= pd):
                continue
        visited[current] = (hops, total_dist)
        if current == destination:
            return path, hops, total_dist
        for (neighbour, dist, airline) in GRAPH.get(current, []):
            if neighbour in visited:
                ph, pd = visited[neighbour]
                nh, nd = hops + 1, total_dist + dist
                if nh > ph or (nh == ph and nd >= pd):
                    continue
            heapq.heappush(heap, (
                hops + 1, total_dist + dist, neighbour,
                path + [(current, neighbour, dist, airline)]
            ))
    return None, None, None


def dijkstra_all(origin):
    """Returns best[(iata)] = (hops, dist) for all reachable airports."""
    best = {origin: (0, 0.0)}
    heap = [(0, 0.0, origin)]
    while heap:
        hops, dist, current = heapq.heappop(heap)
        b_hops, b_dist = best.get(current, (math.inf, math.inf))
        if hops > b_hops or (hops == b_hops and dist > b_dist):
            continue
        for (neighbour, edge_dist, airline) in GRAPH.get(current, []):
            n_hops, n_dist = hops + 1, dist + edge_dist
            b = best.get(neighbour, (math.inf, math.inf))
            if n_hops < b[0] or (n_hops == b[0] and n_dist < b[1]):
                best[neighbour] = (n_hops, n_dist)
                heapq.heappush(heap, (n_hops, n_dist, neighbour))
    return best


def find_meeting_destinations(attendees, top_n=10, continent_filter=None):
    """
    attendees:        list of {'city': str, 'iatas': [str], 'count': int}
    continent_filter: if set (e.g. 'Europe'), only airports on that continent
                      are considered as candidate destinations.
    Returns (ranked, ranked_home)
    """
    unique_origins = {}
    for a in attendees:
        key = tuple(sorted(a['iatas']))
        unique_origins.setdefault(key, []).append((a['city'], a['count']))

    dist_maps = {}
    for iata_tuple, city_list in unique_origins.items():
        merged = {}
        for origin_iata in iata_tuple:
            if origin_iata not in GRAPH:
                continue
            result = dijkstra_all(origin_iata)
            for dest, (h, d) in result.items():
                cur = merged.get(dest, (math.inf, math.inf, None))
                if h < cur[0] or (h == cur[0] and d < cur[1]):
                    merged[dest] = (h, d, origin_iata)   # track best origin
        dist_maps[iata_tuple] = merged

    all_origin_iatas = set(i for iatas in unique_origins for i in iatas)

    if not dist_maps:
        return [], {}

    candidate_pool = set(list(dist_maps.values())[0].keys())
    for merged in list(dist_maps.values())[1:]:
        candidate_pool &= set(merged.keys())

    # Add home airports as candidates only if they match the continent filter
    # (or if there is no filter). This means home cities in a different continent
    # won't appear in the top 10 when a continent is selected.
    if continent_filter and continent_filter != 'Any':
        matching_home_iatas = {
            iata for iata in all_origin_iatas
            if AIRPORTS.get(iata, {}).get('continent') == continent_filter
        }
        candidate_pool = {
            iata for iata in candidate_pool
            if AIRPORTS.get(iata, {}).get('continent') == continent_filter
        } | matching_home_iatas
        log.info("Continent filter '%s': %d candidates (incl. %d home airports on that continent)",
                 continent_filter, len(candidate_pool), len(matching_home_iatas))
    else:
        # No filter — include all home airports
        candidate_pool |= all_origin_iatas

    candidate_scores = {}
    total_attendees  = sum(a['count'] for a in attendees)

    for dest in candidate_pool:
        total_hops  = 0
        total_dist  = 0.0
        total_price = 0
        total_carbon= 0.0
        reachable   = True
        for iata_tuple, city_list in unique_origins.items():
            total_count = sum(c for _, c in city_list)
            if dest in iata_tuple:
                pass  # home city — zero cost, zero distance
            else:
                cost = dist_maps[iata_tuple].get(dest)
                if cost is None:
                    reachable = False
                    break
                h, d, best_origin = cost
                total_hops  += h * total_count
                total_dist  += d * total_count
                # Estimate return fare and carbon for this group
                oneway_price, oneway_carbon = estimate_fare(d, h, best_origin, dest)
                total_price  += oneway_price  * 2 * total_count
                total_carbon += oneway_carbon * 2 * total_count
        if reachable:
            candidate_scores[dest] = (total_hops, total_dist, total_price, total_carbon)

    # Sort by lowest cost, then lowest carbon
    all_ranked = sorted(candidate_scores.items(), key=lambda x: (x[1][2], x[1][3]))

    seen_cities, ranked = set(), []
    for iata, scores in all_ranked:
        if iata == '__fallback__': continue  # skip sentinel (safety guard)
        info = AIRPORTS.get(iata, {})
        city_key = (info.get('city','').lower(), info.get('country','').lower())
        if city_key not in seen_cities:
            seen_cities.add(city_key)
            total_hops, total_dist, total_price, total_carbon = scores
            ranked.append({
                'iata':         iata,
                'city':         info.get('city', ''),
                'country':      info.get('country', ''),
                'name':         info.get('name', ''),
                'continent':    info.get('continent', 'Unknown'),
                'total_hops':   total_hops,
                'total_dist':   round(total_dist),
                'avg_dist':     round(total_dist / total_attendees),
                'avg_hops':     round(total_hops / total_attendees, 1),
                'est_cost':     round(total_price),
                'est_carbon':   round(total_carbon, 1),
                'avg_cost':     round(total_price / total_attendees),
                'avg_carbon':   round(total_carbon / total_attendees, 1),
                'is_home':      iata in all_origin_iatas,
            })
        if len(ranked) == top_n:
            break

    # Also compute home city rankings
    home_scores = {}
    for a in attendees:
        best_iata  = None
        best_score = (math.inf, math.inf, math.inf, math.inf)
        for iata in a['iatas']:
            score = candidate_scores.get(iata, (math.inf, math.inf, math.inf, math.inf))
            if (score[2], score[3]) < (best_score[2], best_score[3]):
                best_score = score
                best_iata  = iata
        if best_iata and best_score[0] < math.inf:
            city = a['city']
            if city not in home_scores or (best_score[2], best_score[3]) < (home_scores[city][1][2], home_scores[city][1][3]):
                home_scores[city] = (best_iata, best_score, a['count'])

    ranked_home = []
    for city, (iata, scores, local_count) in sorted(
            home_scores.items(), key=lambda x: (x[1][1][2], x[1][1][3])):
        info = AIRPORTS.get(iata, {})
        total_hops, total_dist, total_price, total_carbon = scores
        ranked_home.append({
            'iata':        iata,
            'city':        info.get('city', ''),
            'country':     info.get('country', ''),
            'name':        info.get('name', ''),
            'continent':   info.get('continent', 'Unknown'),
            'home_city':   city,
            'local_count': local_count,
            'total_hops':  total_hops,
            'total_dist':  round(total_dist),
            'avg_dist':    round(total_dist / total_attendees),
            'avg_hops':    round(total_hops / total_attendees, 1),
            'est_cost':    round(total_price),
            'est_carbon':  round(total_carbon, 1),
            'avg_cost':    round(total_price / total_attendees),
            'avg_carbon':  round(total_carbon / total_attendees, 1),
        })

    return ranked, ranked_home


def get_routes_for_destination(attendees, dest_iata):
    """Return per-attendee route details for a given destination."""
    results = []
    for a in attendees:
        if dest_iata in a['iatas']:
            results.append({
                'city':    a['city'],
                'count':   a['count'],
                'home':    True,
                'legs':    [],
                'hops':    0,
                'dist_km': 0,
            })
            continue

        best_path, best_hops, best_dist, best_origin_iata = None, math.inf, math.inf, None
        for origin_iata in a['iatas']:
            path, hops, dist = find_best_route(origin_iata, dest_iata)
            if path is None:
                continue
            if hops < best_hops or (hops == best_hops and dist < best_dist):
                best_path, best_hops, best_dist, best_origin_iata = path, hops, dist, origin_iata

        if best_path is None:
            results.append({'city': a['city'], 'count': a['count'],
                            'home': False, 'error': 'No route found', 'legs': []})
            continue

        legs = []
        for src, dst, dist_km, airline in best_path:
            si = AIRPORTS.get(src, {})
            di = AIRPORTS.get(dst, {})
            legs.append({
                'src': src, 'dst': dst,
                'src_name': si.get('name', src),
                'dst_name': di.get('name', dst),
                'src_city': si.get('city', ''),
                'dst_city': di.get('city', ''),
                'src_country': si.get('country', ''),
                'dst_country': di.get('country', ''),
                'dist_km': round(dist_km),
                'airline': airline,
                'airline_name': AIRLINES.get(airline, airline),
            })
        oneway_price, oneway_carbon = estimate_fare(best_dist, best_hops,
                                                    best_origin_iata, dest_iata)
        results.append({
            'city':             a['city'],
            'count':            a['count'],
            'home':             False,
            'hops':             best_hops,
            'dist_km':          round(best_dist),
            'est_price_person': oneway_price * 2,
            'est_price_group':  oneway_price * 2 * a['count'],
            'est_carbon_person':round(oneway_carbon * 2, 1),
            'est_carbon_group': round(oneway_carbon * 2 * a['count'], 1),
            'legs':             legs,
        })
    return results


# ---------------------------------------------------------------------------
# Distance-based fare estimator
# ---------------------------------------------------------------------------
# Pricing model based on distance bands, reflecting real-world airline economics:
#   - Short haul: high per-km cost (fixed costs dominate)
#   - Medium haul: moderate per-km cost
#   - Long haul: lower per-km cost (economies of scale)
#   - Ultra long haul: slight premium (fewer competitors, premium for length)
#
# Calibrated roughly against typical economy return fares.
# A connection penalty is added per stop to reflect the reality that
# itineraries with connections are rarely as cheap as direct flights suggest.
#
# Two additional adjustments are applied when origin/dest IATAs are known:
#
#   1. Region-pair multiplier — captures competitive dynamics between
#      continent pairs (e.g. intra-Europe LCC market ~0.55×, thin
#      intra-Africa routes ~1.25×).
#
#   2. Hub competition discount — routes between two major international
#      hubs attract far more carriers than thin routes, so a 12% discount
#      is applied when both endpoints are in the top-hub set.

# Top ~50 globally connected hubs that attract significant carrier competition.
_TOP_HUBS = frozenset({
    # Europe
    'LHR', 'LGW', 'CDG', 'AMS', 'FRA', 'MUC', 'MAD', 'BCN', 'FCO', 'MXP',
    'ZRH', 'VIE', 'BRU', 'ARN', 'CPH', 'OSL', 'HEL', 'LIS', 'ATH', 'DUB',
    # North America
    'JFK', 'LAX', 'ORD', 'ATL', 'DFW', 'MIA', 'SFO', 'BOS', 'YYZ', 'EWR',
    'IAD', 'SEA',
    # Middle East (classified as Asia by get_continent)
    'DXB', 'DOH', 'AUH', 'IST',
    # Asia
    'SIN', 'HKG', 'NRT', 'ICN', 'PEK', 'PVG', 'BKK', 'KUL', 'CGK', 'DEL', 'BOM',
    # Oceania
    'SYD', 'MEL',
    # Africa
    'JNB', 'NBO', 'CAI', 'CMN', 'ADD',
    # South America
    'GRU', 'EZE', 'BOG', 'LIM', 'SCL',
})

# Region-pair multipliers — keyed by frozenset so A→B == B→A.
# Same-continent pairs use a single-element frozenset.
_REGION_PAIR_MULTIPLIERS = {
    frozenset({'Europe'}):                          0.55,  # LCC-dominated (Ryanair/EasyJet)
    frozenset({'North America'}):                   0.70,  # cheap US/Canada domestic
    frozenset({'Asia'}):                            0.85,  # SE Asia LCC market
    frozenset({'Africa'}):                          1.25,  # thin routes, limited competition
    frozenset({'South America'}):                   1.10,
    frozenset({'Oceania'}):                         1.05,  # Pacific island routes
    frozenset({'Europe',        'North America'}):  0.90,  # competitive transatlantic
    frozenset({'Europe',        'Asia'}):           0.90,  # Gulf carriers + European airlines
    frozenset({'Europe',        'Africa'}):         1.05,
    frozenset({'Europe',        'South America'}):  1.00,
    frozenset({'Europe',        'Oceania'}):        1.00,
    frozenset({'North America', 'Asia'}):           0.95,
    frozenset({'North America', 'South America'}):  1.10,
    frozenset({'North America', 'Africa'}):         1.20,
    frozenset({'North America', 'Oceania'}):        1.00,
    frozenset({'Asia',          'Africa'}):         1.15,
    frozenset({'Asia',          'South America'}):  1.20,
    frozenset({'Asia',          'Oceania'}):        1.00,
    frozenset({'Africa',        'South America'}):  1.30,
    frozenset({'Africa',        'Oceania'}):        1.20,
    frozenset({'South America', 'Oceania'}):        1.10,
}


def _route_price_factor(origin_iata, dest_iata):
    """
    Combined price adjustment factor for a specific city pair.

    Multiplies the raw distance-band estimate by:
      • a region-pair multiplier (competitive dynamics)
      • a hub discount (0.88×) when both endpoints are major hubs
    Returns 1.0 when either IATA is unknown.
    """
    if not origin_iata or not dest_iata:
        return 1.0

    o_cont = AIRPORTS.get(origin_iata, {}).get('continent', '')
    d_cont = AIRPORTS.get(dest_iata,   {}).get('continent', '')

    region_factor = _REGION_PAIR_MULTIPLIERS.get(frozenset({o_cont, d_cont}), 1.0) \
                    if o_cont and d_cont else 1.0

    hub_factor = 0.88 if (origin_iata in _TOP_HUBS and dest_iata in _TOP_HUBS) else 1.0

    return region_factor * hub_factor


def _carbon_factor(dist_km):
    """
    kg CO₂ per passenger-km for economy class, without radiative forcing index.
    Matches Google Flights / ICAO methodology.
    Short-haul flights are less efficient per km (smaller aircraft, higher
    takeoff/landing share); long-haul widebodies at high load factors are
    considerably more efficient.
    """
    if dist_km <= 750:
        return 0.170   # Regional / short-haul narrow-body
    elif dist_km <= 2000:
        return 0.130   # Short-to-medium haul
    elif dist_km <= 5000:
        return 0.105   # Medium haul
    elif dist_km <= 9000:
        return 0.095   # Long haul (widebody, ~85% load factor)
    else:
        return 0.085   # Ultra long haul (very efficient widebody)


def estimate_fare(total_dist_km, num_stops, origin_iata=None, dest_iata=None):
    """
    Estimate a one-way economy fare in USD based on total route distance
    and number of stops. Returns (price_usd, carbon_kg_oneway).

    When origin_iata and dest_iata are supplied a region-pair multiplier
    and hub-competition discount are applied to the base fare.
    """
    d = total_dist_km

    # Base fare by distance band
    if d <= 500:
        # Very short haul — fixed costs dominate
        base = 60 + d * 0.18
    elif d <= 1500:
        # Short haul
        base = 80 + d * 0.14
    elif d <= 4000:
        # Medium haul
        base = 130 + d * 0.10
    elif d <= 8000:
        # Long haul
        base = 250 + d * 0.07
    elif d <= 12000:
        # Very long haul
        base = 350 + d * 0.065
    else:
        # Ultra long haul
        base = 450 + d * 0.06

    # Connection penalty — each stop adds ~$60 (taxes, inconvenience premium)
    connection_penalty = num_stops * 60

    price_factor = _route_price_factor(origin_iata, dest_iata)
    one_way = round((base + connection_penalty) * price_factor)
    carbon  = round(d * _carbon_factor(d), 1)

    return one_way, carbon


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=_HERE)

@app.route('/')
def index():
    return send_from_directory(_HERE, 'index.html')

@app.route('/world-airports.svg')
def world_map():
    return send_from_directory(_HERE, 'world-airports.svg', mimetype='image/svg+xml')

@app.route('/favicon.svg')
def favicon():
    return send_from_directory(_HERE, 'favicon.svg', mimetype='image/svg+xml')

@app.route('/api/search_city')
def search_city():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    return jsonify(find_airports_by_city(q))

@app.route('/api/find_destinations', methods=['POST'])
def find_destinations():
    data              = request.json
    attendees         = data.get('attendees', [])
    continent_filter  = data.get('continent_filter', None)
    if len(attendees) < 2:
        return jsonify({'error': 'Please add at least 2 attendees.'}), 400
    log.info("find_destinations: %d attendees, continent_filter=%s",
             len(attendees), continent_filter)
    ranked, ranked_home = find_meeting_destinations(
        attendees, continent_filter=continent_filter)
    return jsonify({'overall': ranked, 'home': ranked_home,
                    'continent_filter': continent_filter})

@app.route('/api/get_routes', methods=['POST'])
def get_routes():
    data      = request.json
    attendees = data.get('attendees', [])
    dest_iata = data.get('dest_iata', '')
    if not dest_iata or not attendees:
        return jsonify({'error': 'Missing data.'}), 400
    dest_info = AIRPORTS.get(dest_iata, {})
    routes    = get_routes_for_destination(attendees, dest_iata)
    return jsonify({'dest': dest_info, 'dest_iata': dest_iata, 'routes': routes})

@app.route('/api/get_live_prices', methods=['POST'])
def get_live_prices():
    """
    Fetch real-time return fares from SerpApi Google Flights.
    Falls back to distance estimate if SerpApi key not configured.
    """
    data        = request.json
    attendees   = data.get('attendees', [])
    dest_iata   = data.get('dest_iata', '')
    weeks_ahead = int(data.get('weeks_ahead', 8))

    if not dest_iata or not attendees:
        return jsonify({'error': 'Missing data.'}), 400

    if SERPAPI_KEY == "YOUR_SERPAPI_KEY_HERE":
        return jsonify({'error': 'SerpApi key not configured.'}), 400

    from datetime import date, timedelta
    outbound     = date.today() + timedelta(weeks=weeks_ahead)
    return_d     = outbound + timedelta(days=3)
    outbound_str = outbound.strftime("%Y-%m-%d")
    return_str   = return_d.strftime("%Y-%m-%d")

    log.info("get_live_prices: dest=%s, outbound=%s", dest_iata, outbound_str)

    results      = []
    total_price  = 0
    total_carbon = 0.0

    for a in attendees:
        if dest_iata in a['iatas']:
            results.append({'city': a['city'], 'count': a['count'],
                            'home': True, 'price_per_person': 0,
                            'total_price': 0, 'carbon_kg_person': 0,
                            'carbon_kg_group': 0, 'source': 'home'})
            continue

        best_origin, best_hops, best_dist = None, math.inf, math.inf
        for origin_iata in a['iatas']:
            path, hops, dist = find_best_route(origin_iata, dest_iata)
            if path is None: continue
            if hops < best_hops or (hops == best_hops and dist < best_dist):
                best_origin, best_hops, best_dist = origin_iata, hops, dist

        if best_origin is None:
            results.append({'city': a['city'], 'count': a['count'],
                            'error': 'No route found'})
            continue

        # Try SerpApi
        price_data = serpapi_flight_price(best_origin, dest_iata,
                                          outbound_str, return_str)

        if 'error' in price_data:
            # Fall back to estimate
            oneway_price, oneway_carbon = estimate_fare(best_dist, best_hops,
                                                        best_origin, dest_iata)
            price_per_person = oneway_price * 2
            carbon_per_person = round(oneway_carbon * 2, 1)
            source = 'estimate (SerpApi failed)'
            log.warning("SerpApi failed for %s->%s, using estimate: %s",
                        best_origin, dest_iata, price_data['error'])
        else:
            price_per_person = price_data['price']
            carbon_g         = price_data.get('carbon_g')
            if carbon_g:
                carbon_per_person = round((carbon_g * 2) / 1000, 1)
            else:
                # SerpAPI returned no carbon data — fall back to distance estimate
                _, oneway_carbon  = estimate_fare(best_dist, best_hops,
                                                  best_origin, dest_iata)
                carbon_per_person = round(oneway_carbon * 2, 1)
            source = 'live'

        group_total  = price_per_person * a['count']
        group_carbon = round(carbon_per_person * a['count'], 1) if carbon_per_person else None
        total_price += group_total
        if group_carbon: total_carbon += group_carbon

        results.append({
            'city':             a['city'],
            'count':            a['count'],
            'home':             False,
            'origin':           best_origin,
            'dist_km':          round(best_dist),
            'price_per_person': price_per_person,
            'total_price':      group_total,
            'carbon_kg_person': carbon_per_person,
            'carbon_kg_group':  group_carbon,
            'outbound':         outbound_str,
            'return_date':      return_str,
            'source':           source,
        })

    return jsonify({
        'results':         results,
        'total_price':     total_price,
        'total_carbon_kg': round(total_carbon, 1) if total_carbon else None,
        'dest_iata':       dest_iata,
        'outbound_date':   outbound_str,
        'return_date':     return_str,
    })


def serpapi_flight_price(origin_iata, dest_iata, outbound_date, return_date):
    """Query SerpApi Google Flights for a return fare."""
    params = {
        "engine": "google_flights", "departure_id": origin_iata,
        "arrival_id": dest_iata, "outbound_date": outbound_date,
        "return_date": return_date, "currency": "USD",
        "hl": "en", "type": "1", "api_key": SERPAPI_KEY,
    }
    url = "https://serpapi.com/search?" + urllib.parse.urlencode(params)
    log.info("SerpApi: %s->%s %s", origin_iata, dest_iata, outbound_date)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log.error("SerpApi HTTP %s: %s", e.code, body[:200])
        return {"error": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        log.error("SerpApi error: %s", e)
        return {"error": str(e)}

    if "error" in data:
        return {"error": data["error"]}

    price, carbon_g, best_flight = None, None, None
    for section in ("best_flights", "other_flights"):
        for flight in data.get(section, []):
            p = flight.get("price")
            if p is not None:
                try:
                    p = int(p)
                    if price is None or p < price:
                        price, best_flight = p, flight
                except (ValueError, TypeError):
                    pass

    if price is None:
        price = data.get("price_insights", {}).get("lowest_price")

    if price is None:
        log.warning("No price in SerpApi response for %s->%s", origin_iata, dest_iata)
        return {"error": "No prices found"}

    if best_flight:
        raw_co2 = (best_flight.get("carbon_emissions") or {}).get("this_flight")
        if raw_co2:
            try: carbon_g = int(raw_co2)
            except (ValueError, TypeError): pass

    log.info("SerpApi price: $%d, carbon: %sg for %s->%s", price, carbon_g, origin_iata, dest_iata)
    return {"price": price, "carbon_g": carbon_g, "currency": "USD"}


if __name__ == '__main__':
    port  = int(os.environ.get('PORT', 5001))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    host  = '0.0.0.0' if not debug else '127.0.0.1'
    print(f"\nStarting server at http://{host}:{port}  (debug={debug})\n")
    app.run(debug=debug, host=host, port=port, use_reloader=False)
