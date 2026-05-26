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
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

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

# ---------------------------------------------------------------------------
# European Rail Network — Phase 1
# ---------------------------------------------------------------------------
# One station node per city (5-char: 2-char ISO country + 3-char city abbrev).
# Rail routes are kept in RAIL_GRAPH, separate from the air GRAPH, so
# existing air-routing code is unchanged.  Airport→rail mappings let the
# scoring and route-detail functions compare rail vs air for each leg.
# ---------------------------------------------------------------------------

RAIL_CARBON_FACTOR = 0.006   # kg CO₂/pax-km for European high-speed rail
                              # (IEA/EEA figure for electrified HSR, no RFI)

RAIL_STATIONS = {
    # ── United Kingdom ──────────────────────────────────────────────────────
    'GBLON': {'name': 'London St Pancras',    'city': 'London',       'country': 'United Kingdom'},
    'GBEDB': {'name': 'Edinburgh Waverley',   'city': 'Edinburgh',    'country': 'United Kingdom'},
    'GBGLA': {'name': 'Glasgow Central',      'city': 'Glasgow',      'country': 'United Kingdom'},
    'GBMAN': {'name': 'Manchester Piccadilly','city': 'Manchester',   'country': 'United Kingdom'},
    'GBBHM': {'name': 'Birmingham New St',    'city': 'Birmingham',   'country': 'United Kingdom'},
    'GBBRS': {'name': 'Bristol Temple Meads', 'city': 'Bristol',      'country': 'United Kingdom'},
    # ── France ──────────────────────────────────────────────────────────────
    'FRPAR': {'name': 'Paris Gare du Nord',   'city': 'Paris',        'country': 'France'},
    'FRLYS': {'name': 'Lyon Part-Dieu',       'city': 'Lyon',         'country': 'France'},
    'FRMRS': {'name': 'Marseille St-Charles', 'city': 'Marseille',    'country': 'France'},
    'FRNIC': {'name': 'Nice-Ville',           'city': 'Nice',         'country': 'France'},
    'FRBOD': {'name': 'Bordeaux St-Jean',     'city': 'Bordeaux',     'country': 'France'},
    'FRTLS': {'name': 'Toulouse Matabiau',    'city': 'Toulouse',     'country': 'France'},
    'FRSXB': {'name': 'Strasbourg',           'city': 'Strasbourg',   'country': 'France'},
    'FRNTE': {'name': 'Nantes',               'city': 'Nantes',       'country': 'France'},
    # ── Belgium / Netherlands ───────────────────────────────────────────────
    'BEBRU': {'name': 'Brussels-Midi',        'city': 'Brussels',     'country': 'Belgium'},
    'NLAMS': {'name': 'Amsterdam Centraal',   'city': 'Amsterdam',    'country': 'Netherlands'},
    'NLRTM': {'name': 'Rotterdam Centraal',   'city': 'Rotterdam',    'country': 'Netherlands'},
    # ── Germany ─────────────────────────────────────────────────────────────
    'DEFRA': {'name': 'Frankfurt Hbf',        'city': 'Frankfurt',    'country': 'Germany'},
    'DEBER': {'name': 'Berlin Hbf',           'city': 'Berlin',       'country': 'Germany'},
    'DEMUC': {'name': 'Munich Hbf',           'city': 'Munich',       'country': 'Germany'},
    'DEHAM': {'name': 'Hamburg Hbf',          'city': 'Hamburg',      'country': 'Germany'},
    'DECGN': {'name': 'Cologne Hbf',          'city': 'Cologne',      'country': 'Germany'},
    'DESTT': {'name': 'Stuttgart Hbf',        'city': 'Stuttgart',    'country': 'Germany'},
    'DEDUS': {'name': 'Düsseldorf Hbf',       'city': 'Düsseldorf',   'country': 'Germany'},
    'DENUR': {'name': 'Nuremberg Hbf',        'city': 'Nuremberg',    'country': 'Germany'},
    'DEHAN': {'name': 'Hannover Hbf',         'city': 'Hannover',     'country': 'Germany'},
    # ── Switzerland ─────────────────────────────────────────────────────────
    'CHZRH': {'name': 'Zurich Hbf',           'city': 'Zurich',       'country': 'Switzerland'},
    'CHGVA': {'name': 'Geneva Cornavin',      'city': 'Geneva',       'country': 'Switzerland'},
    'CHBSL': {'name': 'Basel SBB',            'city': 'Basel',        'country': 'Switzerland'},
    'CHBRN': {'name': 'Bern',                 'city': 'Bern',         'country': 'Switzerland'},
    # ── Austria ─────────────────────────────────────────────────────────────
    'ATVIE': {'name': 'Vienna Hbf',           'city': 'Vienna',       'country': 'Austria'},
    'ATSBG': {'name': 'Salzburg Hbf',         'city': 'Salzburg',     'country': 'Austria'},
    'ATGRZ': {'name': 'Graz Hbf',             'city': 'Graz',         'country': 'Austria'},
    # ── Italy ───────────────────────────────────────────────────────────────
    'ITMIL': {'name': 'Milan Centrale',       'city': 'Milan',        'country': 'Italy'},
    'ITROM': {'name': 'Rome Termini',         'city': 'Rome',         'country': 'Italy'},
    'ITTRN': {'name': 'Turin Porta Nuova',    'city': 'Turin',        'country': 'Italy'},
    'ITFLO': {'name': 'Florence SMN',         'city': 'Florence',     'country': 'Italy'},
    'ITVCE': {'name': 'Venice Santa Lucia',   'city': 'Venice',       'country': 'Italy'},
    'ITNAP': {'name': 'Naples Centrale',      'city': 'Naples',       'country': 'Italy'},
    'ITBLN': {'name': 'Bologna Centrale',     'city': 'Bologna',      'country': 'Italy'},
    # ── Spain / Portugal ────────────────────────────────────────────────────
    'ESMAD': {'name': 'Madrid Atocha',        'city': 'Madrid',       'country': 'Spain'},
    'ESBCN': {'name': 'Barcelona Sants',      'city': 'Barcelona',    'country': 'Spain'},
    'ESSVQ': {'name': 'Seville Santa Justa',  'city': 'Seville',      'country': 'Spain'},
    'ESVLC': {'name': 'Valencia Joaquín Sorolla', 'city': 'Valencia', 'country': 'Spain'},
    'ESMLG': {'name': 'Málaga Maria Zambrano','city': 'Málaga',       'country': 'Spain'},
    'PTLIS': {'name': 'Lisbon Oriente',       'city': 'Lisbon',       'country': 'Portugal'},
    'PTOPO': {'name': 'Porto Campanhã',       'city': 'Porto',        'country': 'Portugal'},
    # ── Czech Republic / Slovakia ────────────────────────────────────────────
    'CZPRG': {'name': 'Prague hl.n.',         'city': 'Prague',       'country': 'Czech Republic'},
    'CZBRQ': {'name': 'Brno hl.n.',           'city': 'Brno',         'country': 'Czech Republic'},
    'SKBTS': {'name': 'Bratislava hl.st.',    'city': 'Bratislava',   'country': 'Slovakia'},
    # ── Hungary / Romania ────────────────────────────────────────────────────
    'HUBUD': {'name': 'Budapest Keleti',      'city': 'Budapest',     'country': 'Hungary'},
    'ROBUH': {'name': 'Bucharest Nord',       'city': 'Bucharest',    'country': 'Romania'},
    # ── Poland ──────────────────────────────────────────────────────────────
    'PLWAW': {'name': 'Warsaw Centralna',     'city': 'Warsaw',       'country': 'Poland'},
    'PLKRK': {'name': 'Kraków Główny',        'city': 'Kraków',       'country': 'Poland'},
    'PLWRO': {'name': 'Wrocław Główny',       'city': 'Wrocław',      'country': 'Poland'},
    'PLGDN': {'name': 'Gdańsk Główny',        'city': 'Gdańsk',       'country': 'Poland'},
    # ── Western Balkans / Southeast Europe ──────────────────────────────────
    'SILJB': {'name': 'Ljubljana',            'city': 'Ljubljana',    'country': 'Slovenia'},
    'HRZAG': {'name': 'Zagreb Glavni kol.',   'city': 'Zagreb',       'country': 'Croatia'},
    'RSBEG': {'name': 'Belgrade Centar',      'city': 'Belgrade',     'country': 'Serbia'},
    'BGSFP': {'name': 'Sofia',                'city': 'Sofia',        'country': 'Bulgaria'},
    'GRTHE': {'name': 'Thessaloniki',         'city': 'Thessaloniki', 'country': 'Greece'},
    'GRATH': {'name': 'Athens Larissa',       'city': 'Athens',       'country': 'Greece'},
    'TRIST': {'name': 'Istanbul Halkali',     'city': 'Istanbul',     'country': 'Turkey'},
    # ── Scandinavia ─────────────────────────────────────────────────────────
    'SESTO': {'name': 'Stockholm Centralen',  'city': 'Stockholm',    'country': 'Sweden'},
    'SEGOT': {'name': 'Gothenburg Centralen', 'city': 'Gothenburg',   'country': 'Sweden'},
    'SEMAL': {'name': 'Malmö Centralen',      'city': 'Malmö',        'country': 'Sweden'},
    'DKCPH': {'name': 'Copenhagen H',         'city': 'Copenhagen',   'country': 'Denmark'},
    'NOOSL': {'name': 'Oslo S',               'city': 'Oslo',         'country': 'Norway'},
    # ── Baltic ──────────────────────────────────────────────────────────────
    'EETAL': {'name': 'Tallinn',              'city': 'Tallinn',      'country': 'Estonia'},
    'LVRIX': {'name': 'Riga',                 'city': 'Riga',         'country': 'Latvia'},
    'LTVNO': {'name': 'Vilnius',              'city': 'Vilnius',      'country': 'Lithuania'},
}

# Airport IATA → nearest rail station (same metro area)
AIRPORT_TO_RAIL = {
    # ── United Kingdom ──────────────────────────────────────────────────────
    'LHR': 'GBLON', 'LGW': 'GBLON', 'STN': 'GBLON', 'LTN': 'GBLON', 'LCY': 'GBLON',
    'EDI': 'GBEDB',
    'GLA': 'GBGLA',
    'MAN': 'GBMAN',
    'BHX': 'GBBHM',
    'BRS': 'GBBRS',
    # ── France ──────────────────────────────────────────────────────────────
    'CDG': 'FRPAR', 'ORY': 'FRPAR',
    'LYS': 'FRLYS',
    'MRS': 'FRMRS',
    'NCE': 'FRNIC',
    'BOD': 'FRBOD',
    'TLS': 'FRTLS',
    'SXB': 'FRSXB',
    'NTE': 'FRNTE',
    # ── Belgium / Netherlands ───────────────────────────────────────────────
    'BRU': 'BEBRU',
    'AMS': 'NLAMS',
    'RTM': 'NLRTM',
    # ── Germany ─────────────────────────────────────────────────────────────
    'FRA': 'DEFRA',
    'BER': 'DEBER', 'TXL': 'DEBER',
    'MUC': 'DEMUC',
    'HAM': 'DEHAM',
    'CGN': 'DECGN',
    'STR': 'DESTT',
    'DUS': 'DEDUS',
    'NUE': 'DENUR',
    'HAJ': 'DEHAN',
    # ── Switzerland ─────────────────────────────────────────────────────────
    'ZRH': 'CHZRH',
    'GVA': 'CHGVA',
    'BSL': 'CHBSL', 'EAP': 'CHBSL', 'MLH': 'CHBSL',
    'BRN': 'CHBRN',
    # ── Austria ─────────────────────────────────────────────────────────────
    'VIE': 'ATVIE',
    'SZG': 'ATSBG',
    'GRZ': 'ATGRZ',
    # ── Italy ───────────────────────────────────────────────────────────────
    'MXP': 'ITMIL', 'LIN': 'ITMIL',
    'FCO': 'ITROM', 'CIA': 'ITROM',
    'TRN': 'ITTRN',
    'FLR': 'ITFLO',
    'VCE': 'ITVCE', 'TSF': 'ITVCE',
    'NAP': 'ITNAP',
    'BLQ': 'ITBLN',
    # ── Spain / Portugal ────────────────────────────────────────────────────
    'MAD': 'ESMAD',
    'BCN': 'ESBCN',
    'SVQ': 'ESSVQ',
    'VLC': 'ESVLC',
    'AGP': 'ESMLG',
    'LIS': 'PTLIS',
    'OPO': 'PTOPO',
    # ── Czech Republic / Slovakia ────────────────────────────────────────────
    'PRG': 'CZPRG',
    'BRQ': 'CZBRQ',
    'BTS': 'SKBTS',
    # ── Hungary / Romania ────────────────────────────────────────────────────
    'BUD': 'HUBUD',
    'OTP': 'ROBUH', 'BBU': 'ROBUH',
    # ── Poland ──────────────────────────────────────────────────────────────
    'WAW': 'PLWAW',
    'KRK': 'PLKRK',
    'WRO': 'PLWRO',
    'GDN': 'PLGDN',
    # ── Western Balkans / Southeast Europe ──────────────────────────────────
    'LJU': 'SILJB',
    'ZAG': 'HRZAG',
    'BEG': 'RSBEG',
    'SOF': 'BGSFP',
    'SKG': 'GRTHE',
    'ATH': 'GRATH',
    'IST': 'TRIST', 'SAW': 'TRIST',
    # ── Scandinavia ─────────────────────────────────────────────────────────
    'ARN': 'SESTO', 'NYO': 'SESTO',
    'GOT': 'SEGOT',
    'MMX': 'SEMAL',
    'CPH': 'DKCPH',
    'OSL': 'NOOSL',
    # ── Baltic ──────────────────────────────────────────────────────────────
    'TLL': 'EETAL',
    'RIX': 'LVRIX',
    'VNO': 'LTVNO',
}

# Reverse lookup: rail station → list of served airport IATAs
RAIL_TO_AIRPORTS = defaultdict(list)
for _iata, _rail in AIRPORT_TO_RAIL.items():
    RAIL_TO_AIRPORTS[_rail].append(_iata)

# High-speed and main-line rail connections (bidirectional, distances in km).
# Covers the Interrail network across the UK and Europe.
_RAIL_EDGES = [
    # ── UK internal (West Coast, East Coast, Great Western Main Lines) ──────
    ('GBLON', 'GBBHM',  180, 'Avanti/CrossCountry'),
    ('GBLON', 'GBMAN',  295, 'Avanti West Coast'),
    ('GBLON', 'GBBRS',  190, 'GWR'),
    ('GBLON', 'GBEDB',  630, 'LNER'),
    ('GBLON', 'GBGLA',  645, 'Avanti West Coast'),
    ('GBBHM', 'GBMAN',  130, 'Avanti/Transpennine'),
    ('GBBHM', 'GBEDB',  450, 'CrossCountry'),
    ('GBMAN', 'GBEDB',  335, 'Transpennine/LNER'),
    ('GBMAN', 'GBGLA',  345, 'Avanti West Coast'),
    ('GBEDB', 'GBGLA',   75, 'ScotRail'),
    # ── Channel Tunnel ───────────────────────────────────────────────────────
    ('GBLON', 'FRPAR',  493, 'Eurostar'),
    ('GBLON', 'BEBRU',  370, 'Eurostar'),
    # ── France internal (TGV) ────────────────────────────────────────────────
    ('FRPAR', 'FRLYS',  465, 'TGV'),
    ('FRPAR', 'FRMRS',  863, 'TGV'),
    ('FRPAR', 'FRNIC',  930, 'TGV'),
    ('FRPAR', 'FRBOD',  580, 'TGV'),
    ('FRPAR', 'FRTLS',  680, 'TGV'),
    ('FRPAR', 'FRNTE',  385, 'TGV'),
    ('FRPAR', 'FRSXB',  490, 'TGV'),
    ('FRLYS', 'FRMRS',  315, 'TGV'),
    ('FRLYS', 'FRNIC',  430, 'TGV'),
    ('FRLYS', 'FRTLS',  530, 'TGV'),
    ('FRLYS', 'CHGVA',  155, 'TGV/IC'),
    ('FRLYS', 'ITMIL',  400, 'TGV/Frecciarossa'),
    ('FRMRS', 'FRNIC',  200, 'TGV'),
    # ── France / Benelux / Germany ───────────────────────────────────────────
    ('FRPAR', 'BEBRU',  312, 'Eurostar/Thalys'),
    ('FRPAR', 'NLAMS',  503, 'Thalys'),
    ('FRPAR', 'DEFRA',  579, 'TGV/ICE'),
    ('FRPAR', 'CHZRH',  601, 'TGV'),
    ('FRPAR', 'CHGVA',  501, 'TGV'),
    ('FRPAR', 'ESBCN', 1040, 'TGV/AVE'),
    ('FRPAR', 'ITMIL',  693, 'TGV/Frecciarossa'),
    ('FRPAR', 'ITTRN',  850, 'TGV/Frecciarossa'),
    ('FRSXB', 'DEFRA',  220, 'TGV/ICE'),
    ('FRSXB', 'CHBSL',   80, 'TER/IC'),
    ('BEBRU', 'NLAMS',  192, 'Thalys'),
    ('BEBRU', 'DEFRA',  496, 'ICE/Thalys'),
    ('BEBRU', 'DECGN',  220, 'Thalys'),
    ('NLAMS', 'DEFRA',  487, 'ICE'),
    ('NLAMS', 'DEBER',  648, 'ICE'),
    ('NLAMS', 'DECGN',  260, 'ICE/Thalys'),
    ('NLRTM', 'NLAMS',   80, 'Intercity'),
    ('NLRTM', 'BEBRU',  140, 'Thalys'),
    ('NLRTM', 'DECGN',  230, 'ICE'),
    # ── Germany internal (ICE network) ──────────────────────────────────────
    ('DEFRA', 'DEBER',  557, 'ICE'),
    ('DEFRA', 'DEMUC',  302, 'ICE'),
    ('DEFRA', 'ATVIE',  744, 'ICE'),
    ('DEFRA', 'CHZRH',  368, 'ICE'),
    ('DEFRA', 'DECGN',  190, 'ICE'),
    ('DEFRA', 'DESTT',  200, 'ICE'),
    ('DEFRA', 'DEDUS',  240, 'ICE'),
    ('DEFRA', 'DEHAN',  375, 'ICE'),
    ('DEFRA', 'DENUR',  225, 'ICE'),
    ('DEBER', 'DEHAM',  289, 'ICE'),
    ('DEBER', 'CZPRG',  353, 'EC'),
    ('DEBER', 'PLWAW',  573, 'ICE'),
    ('DEBER', 'DECGN',  580, 'ICE'),
    ('DEBER', 'DENUR',  435, 'ICE'),
    ('DEBER', 'DEHAN',  290, 'ICE'),
    ('DEHAM', 'DKCPH',  361, 'ICE'),
    ('DEHAM', 'DEHAN',  150, 'ICE'),
    ('DEHAM', 'DECGN',  420, 'ICE'),
    ('DECGN', 'DEDUS',   45, 'RE/ICE'),
    ('DEDUS', 'DEHAN',  290, 'ICE'),
    ('DEDUS', 'DEHAM',  380, 'ICE'),
    ('DEHAN', 'DEBER',  290, 'ICE'),
    ('DEHAN', 'DECGN',  290, 'ICE'),
    ('DEHAN', 'DENUR',  390, 'ICE'),
    ('DEMUC', 'CHZRH',  319, 'EC/ICE'),
    ('DEMUC', 'ATVIE',  379, 'Railjet'),
    ('DEMUC', 'ITMIL',  514, 'ICE/EC'),
    ('DEMUC', 'DESTT',  225, 'ICE'),
    ('DEMUC', 'DENUR',  165, 'ICE'),
    ('DEMUC', 'ATSBG',  150, 'ICE/Railjet'),
    ('DESTT', 'CHZRH',  200, 'ICE'),
    # ── Switzerland ─────────────────────────────────────────────────────────
    ('CHZRH', 'ITMIL',  294, 'EC'),
    ('CHZRH', 'CHGVA',  236, 'IC'),
    ('CHZRH', 'CHBSL',   85, 'IC'),
    ('CHZRH', 'CHBRN',  125, 'IC'),
    ('CHGVA', 'CHBRN',  165, 'IC'),
    ('CHGVA', 'FRNIC',  370, 'TGV'),
    ('CHBRN', 'CHBSL',  100, 'IC'),
    ('CHBSL', 'DEFRA',  280, 'ICE'),
    # ── Austria ─────────────────────────────────────────────────────────────
    ('ATVIE', 'HUBUD',  243, 'Railjet'),
    ('ATVIE', 'CZPRG',  323, 'Railjet'),
    ('ATVIE', 'ATSBG',  300, 'Railjet'),
    ('ATVIE', 'ATGRZ',  200, 'Railjet'),
    ('ATVIE', 'SKBTS',   65, 'Railjet/EC'),
    ('ATVIE', 'SILJB',  400, 'EC'),
    ('ATSBG', 'DEMUC',  150, 'ICE/Railjet'),
    ('ATGRZ', 'SILJB',  190, 'EC'),
    ('ATGRZ', 'HRZAG',  250, 'EC'),
    # ── Italy (Frecciarossa / EC) ────────────────────────────────────────────
    ('ITMIL', 'ITTRN',  140, 'Frecciarossa'),
    ('ITMIL', 'ITVCE',  265, 'Frecciarossa'),
    ('ITMIL', 'ITBLN',  210, 'Frecciarossa'),
    ('ITMIL', 'ITFLO',  300, 'Frecciarossa'),
    ('ITMIL', 'ITROM',  572, 'Frecciarossa'),
    ('ITBLN', 'ITFLO',  105, 'Frecciarossa'),
    ('ITBLN', 'ITVCE',  150, 'Frecciarossa'),
    ('ITBLN', 'ITROM',  385, 'Frecciarossa'),
    ('ITFLO', 'ITROM',  280, 'Frecciarossa'),
    ('ITROM', 'ITNAP',  220, 'Frecciarossa'),
    ('ITTRN', 'FRNIC',  215, 'EC/TGV'),
    ('ITVCE', 'ATVIE',  580, 'Nightjet/Railjet'),
    # ── Iberia (AVE / Alfa Pendular) ─────────────────────────────────────────
    ('ESBCN', 'ESMAD',  620, 'AVE'),
    ('ESBCN', 'ESVLC',  350, 'AVE'),
    ('ESMAD', 'ESVLC',  390, 'AVE'),
    ('ESMAD', 'ESSVQ',  470, 'AVE'),
    ('ESMAD', 'ESMLG',  530, 'AVE'),
    ('ESMAD', 'PTLIS',  640, 'Renfe/CP'),
    ('PTLIS', 'PTOPO',  310, 'Alfa Pendular'),
    # ── Central / Eastern Europe ─────────────────────────────────────────────
    ('CZPRG', 'HUBUD',  540, 'EC'),
    ('CZPRG', 'PLWAW',  666, 'EC'),
    ('CZPRG', 'CZBRQ',  205, 'SC/EC'),
    ('CZPRG', 'SKBTS',  330, 'EC'),
    ('SKBTS', 'PLWAW',  555, 'EC'),
    ('HUBUD', 'HRZAG',  370, 'EC/IC'),
    ('HUBUD', 'RSBEG',  380, 'EC'),
    ('HUBUD', 'ROBUH',  780, 'IC/EN'),
    ('SILJB', 'HRZAG',   70, 'EC'),
    ('HRZAG', 'RSBEG',  375, 'EC/IC'),
    # ── Poland ───────────────────────────────────────────────────────────────
    ('PLWAW', 'PLKRK',  295, 'IC/EIC'),
    ('PLWAW', 'PLWRO',  355, 'IC'),
    ('PLWAW', 'PLGDN',  340, 'IC'),
    ('PLKRK', 'CZBRQ',  295, 'EC'),
    ('PLWRO', 'DEBER',  430, 'EC'),
    # ── Southeast Europe / Balkans ───────────────────────────────────────────
    ('RSBEG', 'BGSFP',  400, 'EC'),
    ('BGSFP', 'GRTHE',  565, 'IC'),
    ('BGSFP', 'ROBUH',  390, 'IC'),
    ('BGSFP', 'TRIST',  560, 'EC'),
    ('GRTHE', 'GRATH',  500, 'IC'),
    ('GRTHE', 'TRIST',  560, 'EC'),
    # ── Scandinavia ──────────────────────────────────────────────────────────
    ('DKCPH', 'SESTO',  613, 'SJ/DSB'),
    ('DKCPH', 'SEGOT',  320, 'SJ/DSB'),
    ('SESTO', 'NOOSL',  521, 'NSB'),
    ('SESTO', 'SEGOT',  470, 'SJ X2000'),
    ('SEGOT', 'SEMAL',  290, 'SJ'),
    ('SEMAL', 'DKCPH',   25, 'Öresund'),
    # ── Baltic ───────────────────────────────────────────────────────────────
    ('LTVNO', 'PLWAW',  490, 'IC/EN'),
    ('LTVNO', 'LVRIX',  295, 'IC'),
    ('LVRIX', 'EETAL',  310, 'IC'),
]

RAIL_GRAPH = defaultdict(list)
for _rs, _rd, _dist, _op in _RAIL_EDGES:
    RAIL_GRAPH[_rs].append((_rd, _dist, _op))
    RAIL_GRAPH[_rd].append((_rs, _dist, _op))

print(f"  Rail network: {len(RAIL_STATIONS)} stations, {len(_RAIL_EDGES)} bidirectional connections.")

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

    # Build European rail distance maps — used to offer a rail alternative
    # for short intra-European legs where train beats or closely matches air.
    rail_dist_maps = {}
    for iata_tuple, city_list in unique_origins.items():
        merged_rail = {}
        for origin_iata in iata_tuple:
            rail_origin = AIRPORT_TO_RAIL.get(origin_iata)
            if not rail_origin:
                continue
            rail_result = dijkstra_rail_all(rail_origin)
            for rail_dest_station, (rh, rd) in rail_result.items():
                n_xfr = max(0, rh - 1)  # interchanges = hops − 1
                for dest_airport in RAIL_TO_AIRPORTS.get(rail_dest_station, []):
                    cur = merged_rail.get(dest_airport, (math.inf, math.inf, math.inf, None))
                    if rh < cur[0] or (rh == cur[0] and rd < cur[1]):
                        merged_rail[dest_airport] = (rh, rd, n_xfr, origin_iata)
        rail_dist_maps[iata_tuple] = merged_rail

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
                oneway_price, oneway_carbon = estimate_fare(d, h, best_origin, dest)

                # Check whether a direct European rail service is cheaper or
                # close in price (within 30%).  Multi-hop rail (transfers ≥ 1)
                # is only preferred when it's outright cheaper than air.
                rail_cost = rail_dist_maps.get(iata_tuple, {}).get(dest)
                if rail_cost:
                    rh, rd, n_xfr, _ = rail_cost
                    r_price, r_carbon = estimate_rail_fare(rd, n_xfr)
                    prefer_rail = (
                        (rh == 1 and r_price <= oneway_price * 1.30) or
                        (rh > 1  and r_price <  oneway_price)
                    )
                    if prefer_rail:
                        h, d, oneway_price, oneway_carbon = rh, rd, r_price, r_carbon

                total_hops  += h * total_count
                total_dist  += d * total_count
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
    """
    Return per-attendee route details for a given destination.

    Each result includes a 'mode' field ('air' or 'rail') and each leg
    carries a 'mode' field so the frontend can show ✈ or 🚂 accordingly.
    Rail is preferred for direct connections (1 leg) that are within 30%
    of the air fare, or for multi-leg rail that is outright cheaper.
    """
    results = []
    for a in attendees:
        if dest_iata in a['iatas']:
            results.append({
                'city':    a['city'],
                'count':   a['count'],
                'home':    True,
                'mode':    'home',
                'legs':    [],
                'hops':    0,
                'dist_km': 0,
            })
            continue

        # ── Best air route ──────────────────────────────────────────────
        best_air_path, best_air_hops, best_air_dist, best_air_origin = None, math.inf, math.inf, None
        for origin_iata in a['iatas']:
            path, hops, dist = find_best_route(origin_iata, dest_iata)
            if path is None:
                continue
            if hops < best_air_hops or (hops == best_air_hops and dist < best_air_dist):
                best_air_path, best_air_hops, best_air_dist, best_air_origin = path, hops, dist, origin_iata

        if best_air_path is None:
            results.append({'city': a['city'], 'count': a['count'],
                            'home': False, 'error': 'No route found', 'legs': []})
            continue

        air_price, air_carbon = estimate_fare(best_air_dist, best_air_hops,
                                              best_air_origin, dest_iata)

        # ── Best rail route (if available) ──────────────────────────────
        best_rail_path, best_rail_hops, best_rail_dist, best_rail_origin = None, math.inf, math.inf, None
        for origin_iata in a['iatas']:
            rail_path, rail_hops, rail_dist = find_best_rail_route(origin_iata, dest_iata)
            if rail_path is None:
                continue
            if rail_hops < best_rail_hops or (rail_hops == best_rail_hops and rail_dist < best_rail_dist):
                best_rail_path, best_rail_hops, best_rail_dist, best_rail_origin = \
                    rail_path, rail_hops, rail_dist, origin_iata

        # ── Decide mode ─────────────────────────────────────────────────
        use_rail = False
        if best_rail_path is not None:
            n_xfr = max(0, best_rail_hops - 1)
            rail_price, rail_carbon = estimate_rail_fare(best_rail_dist, n_xfr)
            prefer_rail = (
                (best_rail_hops == 1 and rail_price <= air_price * 1.30) or
                (best_rail_hops > 1  and rail_price <  air_price)
            )
            if prefer_rail:
                use_rail = True

        # ── Build result ─────────────────────────────────────────────────
        if use_rail:
            legs = []
            for rail_src, rail_dst, dist_km, operator in best_rail_path:
                si = RAIL_STATIONS.get(rail_src, {})
                di = RAIL_STATIONS.get(rail_dst, {})
                legs.append({
                    'src':         rail_src,
                    'dst':         rail_dst,
                    'src_name':    si.get('name', rail_src),
                    'dst_name':    di.get('name', rail_dst),
                    'src_city':    si.get('city', ''),
                    'dst_city':    di.get('city', ''),
                    'src_country': si.get('country', ''),
                    'dst_country': di.get('country', ''),
                    'dist_km':     round(dist_km),
                    'airline':     operator,
                    'airline_name':operator,
                    'mode':        'rail',
                })
            results.append({
                'city':             a['city'],
                'count':            a['count'],
                'home':             False,
                'mode':             'rail',
                'hops':             best_rail_hops,
                'dist_km':          round(best_rail_dist),
                'est_price_person': rail_price * 2,
                'est_price_group':  rail_price * 2 * a['count'],
                'est_carbon_person':round(rail_carbon * 2, 1),
                'est_carbon_group': round(rail_carbon * 2 * a['count'], 1),
                'legs':             legs,
            })
        else:
            legs = []
            for src, dst, dist_km, airline in best_air_path:
                si = AIRPORTS.get(src, {})
                di = AIRPORTS.get(dst, {})
                legs.append({
                    'src':         src,
                    'dst':         dst,
                    'src_name':    si.get('name', src),
                    'dst_name':    di.get('name', dst),
                    'src_city':    si.get('city', ''),
                    'dst_city':    di.get('city', ''),
                    'src_country': si.get('country', ''),
                    'dst_country': di.get('country', ''),
                    'dist_km':     round(dist_km),
                    'airline':     airline,
                    'airline_name':AIRLINES.get(airline, airline),
                    'mode':        'air',
                })
            results.append({
                'city':             a['city'],
                'count':            a['count'],
                'home':             False,
                'mode':             'air',
                'hops':             best_air_hops,
                'dist_km':          round(best_air_dist),
                'est_price_person': air_price * 2,
                'est_price_group':  air_price * 2 * a['count'],
                'est_carbon_person':round(air_carbon * 2, 1),
                'est_carbon_group': round(air_carbon * 2 * a['count'], 1),
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
# European rail fare + routing
# ---------------------------------------------------------------------------

def estimate_rail_fare(dist_km, num_transfers=0):
    """
    Estimate a one-way European rail fare in USD.

    Calibrated against typical advance-purchase HSR fares:
      ≤ 300 km: ~$30–50  (short domestic / cross-border)
      ≤ 600 km: ~$55–90  (Eurostar / Thalys / TGV range)
      ≤ 1000 km: ~$80–130 (longer TGV/ICE journeys)
      > 1000 km: ~$110–160 (Paris–Barcelona / Paris–Milan tier)

    num_transfers: number of rail-to-rail interchanges (0 for a direct service).
    Returns (price_usd, carbon_kg_oneway).
    """
    d = dist_km
    if d <= 300:
        base = 15 + d * 0.12
    elif d <= 600:
        base = 25 + d * 0.09
    elif d <= 1000:
        base = 40 + d * 0.075
    else:
        base = 55 + d * 0.065

    transfer_penalty = num_transfers * 15   # $15 per interchange — much cheaper than flight connections
    carbon = round(d * RAIL_CARBON_FACTOR, 1)
    return round(base + transfer_penalty), carbon


def dijkstra_rail_all(origin_station):
    """
    Dijkstra over RAIL_GRAPH from origin_station.
    Returns best[(station_code)] = (hops, total_dist_km) for all reachable stations.
    """
    if origin_station not in RAIL_GRAPH:
        return {}
    best = {origin_station: (0, 0.0)}
    heap = [(0, 0.0, origin_station)]
    while heap:
        hops, dist, current = heapq.heappop(heap)
        b_hops, b_dist = best.get(current, (math.inf, math.inf))
        if hops > b_hops or (hops == b_hops and dist > b_dist):
            continue
        for (neighbour, edge_dist, operator) in RAIL_GRAPH.get(current, []):
            n_hops, n_dist = hops + 1, dist + edge_dist
            b = best.get(neighbour, (math.inf, math.inf))
            if n_hops < b[0] or (n_hops == b[0] and n_dist < b[1]):
                best[neighbour] = (n_hops, n_dist)
                heapq.heappush(heap, (n_hops, n_dist, neighbour))
    return best


def find_best_rail_route(origin_iata, dest_iata):
    """
    Find the best rail route between two airports' cities.

    Maps each IATA to its rail station via AIRPORT_TO_RAIL, then runs
    Dijkstra over RAIL_GRAPH.  Returns:
      (path, hops, total_dist_km)  — path is a list of (src, dst, dist_km, operator)
      (None, None, None)           — if no rail connection exists
    """
    origin_station = AIRPORT_TO_RAIL.get(origin_iata)
    dest_station   = AIRPORT_TO_RAIL.get(dest_iata)
    if not origin_station or not dest_station or origin_station == dest_station:
        return None, None, None

    heap = [(0, 0.0, origin_station, [])]
    visited = {}
    while heap:
        hops, total_dist, current, path = heapq.heappop(heap)
        if current in visited:
            ph, pd = visited[current]
            if hops > ph or (hops == ph and total_dist >= pd):
                continue
        visited[current] = (hops, total_dist)
        if current == dest_station:
            return path, hops, total_dist
        for (neighbour, dist, operator) in RAIL_GRAPH.get(current, []):
            if neighbour in visited:
                ph, pd = visited[neighbour]
                nh, nd = hops + 1, total_dist + dist
                if nh > ph or (nh == ph and nd >= pd):
                    continue
            heapq.heappush(heap, (
                hops + 1, total_dist + dist, neighbour,
                path + [(current, neighbour, dist, operator)]
            ))
    return None, None, None


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=_HERE)

# CORS — restrict to your deployed origin in production via the ALLOWED_ORIGIN
# environment variable (defaults to * for local development).
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', '*')
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGIN}})

# Rate limiting — disabled automatically when app.config["TESTING"] is True.
# Uses in-memory storage (sufficient for a single-worker deployment).
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://",
    enabled=True,
)

# Security response headers
@app.after_request
def add_security_headers(resp):
    resp.headers['X-Content-Type-Options']  = 'nosniff'
    resp.headers['X-Frame-Options']         = 'DENY'
    resp.headers['Referrer-Policy']         = 'strict-origin-when-cross-origin'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )
    return resp


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
@limiter.limit("20/minute")
def find_destinations():
    data              = request.json
    attendees         = data.get('attendees', [])
    continent_filter  = data.get('continent_filter', None)
    if len(attendees) < 2:
        return jsonify({'error': 'Please add at least 2 attendees.'}), 400
    if len(attendees) > 20:
        return jsonify({'error': 'Maximum 20 attendees supported.'}), 400
    log.info("find_destinations: %d attendees, continent_filter=%s",
             len(attendees), continent_filter)
    ranked, ranked_home = find_meeting_destinations(
        attendees, continent_filter=continent_filter)
    return jsonify({'overall': ranked, 'home': ranked_home,
                    'continent_filter': continent_filter})

@app.route('/api/get_routes', methods=['POST'])
@limiter.limit("60/minute")
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
@limiter.limit("20/minute")
def get_live_prices():
    """
    Fetch real-time return fares from SerpApi Google Flights.
    Falls back to distance estimate if SerpApi key not configured.
    """
    data        = request.json
    attendees   = data.get('attendees', [])
    dest_iata   = data.get('dest_iata', '')
    try:
        weeks_ahead = int(data.get('weeks_ahead', 8))
    except (TypeError, ValueError):
        return jsonify({'error': 'weeks_ahead must be an integer.'}), 400
    if not 1 <= weeks_ahead <= 52:
        return jsonify({'error': 'weeks_ahead must be between 1 and 52.'}), 400

    if not dest_iata or not attendees:
        return jsonify({'error': 'Missing data.'}), 400

    if SERPAPI_KEY == "YOUR_SERPAPI_KEY_HERE":
        return jsonify({'error': 'SerpApi key not configured.'}), 400

    from datetime import date, timedelta
    outbound     = date.today() + timedelta(weeks=weeks_ahead)
    return_d     = outbound + timedelta(days=5)
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
        e.read()  # drain body — never logged to avoid leaking key from error responses
        log.error("SerpApi HTTP %s for %s->%s", e.code, origin_iata, dest_iata)
        return {"error": f"HTTP {e.code}"}
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
