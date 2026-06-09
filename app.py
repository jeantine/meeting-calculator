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
    # Iceland — politically and geographically European despite its mid-Atlantic
    # longitude (~63–66 °N, ~13–27 °W), which would otherwise fall inside the
    # Greenland bounding box below.
    if 62 < lat < 68 and -30 < lon < -10:
        return "Europe"

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

# ── Country-level continent overrides ───────────────────────────────────────
# The source CSV uses a geographic convention that classifies Istanbul and most
# of Turkey as Europe because of the historical/cultural association.  For
# travel-industry purposes Turkey belongs in Asia (IATA groups it with the
# Middle East).  Reclassify all Turkish airports so continent filtering works
# consistently — searching "Asia" finds Istanbul, not just eastern Anatolian
# airports, and Istanbul is no longer excluded as a hub when scoring Asian
# destinations like Konya.
_COUNTRY_CONTINENT_OVERRIDES = {
    'Turkey': 'Asia',
}
for _apt in AIRPORTS.values():
    _override = _COUNTRY_CONTINENT_OVERRIDES.get(_apt.get('country'))
    if _override:
        _apt['continent'] = _override

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
    # Stations on green (high-speed) or purple (mainline) lines per Interrail 2026 map
    'GBLON': {'name': 'London St Pancras',    'city': 'London',        'country': 'United Kingdom'},
    'GBEDB': {'name': 'Edinburgh Waverley',   'city': 'Edinburgh',     'country': 'United Kingdom'},
    'GBGLA': {'name': 'Glasgow Central',      'city': 'Glasgow',       'country': 'United Kingdom'},
    'GBMAN': {'name': 'Manchester Piccadilly','city': 'Manchester',    'country': 'United Kingdom'},
    'GBBHM': {'name': 'Birmingham New St',    'city': 'Birmingham',    'country': 'United Kingdom'},
    'GBBRS': {'name': 'Bristol Temple Meads', 'city': 'Bristol',       'country': 'United Kingdom'},
    'GBLED': {'name': 'Leeds',                'city': 'Leeds',         'country': 'United Kingdom'},
    'GBNEW': {'name': 'Newcastle',            'city': 'Newcastle',     'country': 'United Kingdom'},
    'GBLIV': {'name': 'Liverpool Lime St',    'city': 'Liverpool',     'country': 'United Kingdom'},
    'GBCDF': {'name': 'Cardiff Central',      'city': 'Cardiff',       'country': 'United Kingdom'},
    'GBSOU': {'name': 'Southampton Central',  'city': 'Southampton',   'country': 'United Kingdom'},
    # Midland Main Line
    'GBSHF': {'name': 'Sheffield',           'city': 'Sheffield',     'country': 'United Kingdom'},
    'GBNOT': {'name': 'Nottingham',          'city': 'Nottingham',    'country': 'United Kingdom'},
    # Scotland
    'GBABZ': {'name': 'Aberdeen',            'city': 'Aberdeen',      'country': 'United Kingdom'},
    # ── France ──────────────────────────────────────────────────────────────
    'FRPAR': {'name': 'Paris Gare du Nord',   'city': 'Paris',        'country': 'France'},
    'FRLYS': {'name': 'Lyon Part-Dieu',       'city': 'Lyon',         'country': 'France'},
    'FRMRS': {'name': 'Marseille St-Charles', 'city': 'Marseille',    'country': 'France'},
    'FRNIC': {'name': 'Nice-Ville',           'city': 'Nice',         'country': 'France'},
    'FRBOD': {'name': 'Bordeaux St-Jean',     'city': 'Bordeaux',     'country': 'France'},
    'FRTLS': {'name': 'Toulouse Matabiau',    'city': 'Toulouse',     'country': 'France'},
    'FRSXB': {'name': 'Strasbourg',           'city': 'Strasbourg',   'country': 'France'},
    'FRNTE': {'name': 'Nantes',               'city': 'Nantes',       'country': 'France'},
    'FRLIL': {'name': 'Lille-Europe',         'city': 'Lille',        'country': 'France'},
    'FRMPL': {'name': 'Montpellier St-Roch',  'city': 'Montpellier',  'country': 'France'},
    'FRRNS': {'name': 'Rennes',               'city': 'Rennes',       'country': 'France'},
    # ── Belgium / Netherlands ───────────────────────────────────────────────
    'BEBRU': {'name': 'Brussels-Midi',        'city': 'Brussels',     'country': 'Belgium'},
    'NLAMS': {'name': 'Amsterdam Centraal',   'city': 'Amsterdam',    'country': 'Netherlands'},
    'NLRTM': {'name': 'Rotterdam Centraal',   'city': 'Rotterdam',    'country': 'Netherlands'},
    'BEANR': {'name': 'Antwerp Centraal',     'city': 'Antwerp',      'country': 'Belgium'},
    # ── Luxembourg ──────────────────────────────────────────────────────────
    'LULUX': {'name': 'Luxembourg',           'city': 'Luxembourg',   'country': 'Luxembourg'},
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
    'DELEI': {'name': 'Leipzig Hbf',          'city': 'Leipzig',      'country': 'Germany'},
    'DEDRS': {'name': 'Dresden Hbf',          'city': 'Dresden',      'country': 'Germany'},
    # ── Switzerland ─────────────────────────────────────────────────────────
    'CHZRH': {'name': 'Zurich Hbf',           'city': 'Zurich',       'country': 'Switzerland'},
    'CHGVA': {'name': 'Geneva Cornavin',      'city': 'Geneva',       'country': 'Switzerland'},
    'CHBSL': {'name': 'Basel SBB',            'city': 'Basel',        'country': 'Switzerland'},
    'CHBRN': {'name': 'Bern',                 'city': 'Bern',         'country': 'Switzerland'},
    'CHLAS': {'name': 'Lausanne',             'city': 'Lausanne',     'country': 'Switzerland'},
    # ── Austria ─────────────────────────────────────────────────────────────
    'ATVIE': {'name': 'Vienna Hbf',           'city': 'Vienna',       'country': 'Austria'},
    'ATSBG': {'name': 'Salzburg Hbf',         'city': 'Salzburg',     'country': 'Austria'},
    'ATGRZ': {'name': 'Graz Hbf',             'city': 'Graz',         'country': 'Austria'},
    'ATINN': {'name': 'Innsbruck Hbf',        'city': 'Innsbruck',    'country': 'Austria'},
    # ── Italy ───────────────────────────────────────────────────────────────
    'ITMIL': {'name': 'Milan Centrale',       'city': 'Milan',        'country': 'Italy'},
    'ITROM': {'name': 'Rome Termini',         'city': 'Rome',         'country': 'Italy'},
    'ITTRN': {'name': 'Turin Porta Nuova',    'city': 'Turin',        'country': 'Italy'},
    'ITFLO': {'name': 'Florence SMN',         'city': 'Florence',     'country': 'Italy'},
    'ITVCE': {'name': 'Venice Santa Lucia',   'city': 'Venice',       'country': 'Italy'},
    'ITNAP': {'name': 'Naples Centrale',      'city': 'Naples',       'country': 'Italy'},
    'ITBLN': {'name': 'Bologna Centrale',     'city': 'Bologna',      'country': 'Italy'},
    'ITGOA': {'name': 'Genova Piazza Principe', 'city': 'Genoa',      'country': 'Italy'},
    'ITVRS': {'name': 'Verona Porta Nuova',   'city': 'Verona',       'country': 'Italy'},
    # ── Monaco ──────────────────────────────────────────────────────────────
    'MCMON': {'name': 'Monaco-Monte-Carlo',   'city': 'Monaco',       'country': 'Monaco'},
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

# City → transport options.  Each entry lists the IATA airports that serve
# the city and the rail station code (same as the RAIL_STATIONS key).
# Cities without airports (e.g. Sheffield, Oxford) have airports=[].
# This is the single source of truth; IATA_TO_CITY and STATION_TO_IATAS
# are derived from it at startup.
CITIES = {
    # ── United Kingdom ──────────────────────────────────────────────────────
    # Stations on green (high-speed) or purple (mainline) lines per Interrail 2026 map
    'GBLON': {'name': 'London',       'country': 'United Kingdom',
              'airports': ['LHR', 'LGW', 'STN', 'LTN', 'LCY'], 'rail': 'GBLON'},
    'GBEDB': {'name': 'Edinburgh',    'country': 'United Kingdom',
              'airports': ['EDI'],    'rail': 'GBEDB'},
    'GBGLA': {'name': 'Glasgow',      'country': 'United Kingdom',
              'airports': ['GLA'],    'rail': 'GBGLA'},
    'GBMAN': {'name': 'Manchester',   'country': 'United Kingdom',
              'airports': ['MAN'],    'rail': 'GBMAN'},
    'GBBHM': {'name': 'Birmingham',   'country': 'United Kingdom',
              'airports': ['BHX'],    'rail': 'GBBHM'},
    'GBBRS': {'name': 'Bristol',      'country': 'United Kingdom',
              'airports': ['BRS'],    'rail': 'GBBRS'},
    'GBLED': {'name': 'Leeds',        'country': 'United Kingdom',
              'airports': ['LBA'],    'rail': 'GBLED'},
    'GBNEW': {'name': 'Newcastle',    'country': 'United Kingdom',
              'airports': ['NCL'],    'rail': 'GBNEW'},
    'GBLIV': {'name': 'Liverpool',    'country': 'United Kingdom',
              'airports': ['LPL'],    'rail': 'GBLIV'},
    'GBCDF': {'name': 'Cardiff',      'country': 'United Kingdom',
              'airports': ['CWL'],    'rail': 'GBCDF'},
    'GBSOU': {'name': 'Southampton',  'country': 'United Kingdom',
              'airports': ['SOU'],    'rail': 'GBSOU'},
    # Midland Main Line
    'GBSHF': {'name': 'Sheffield',    'country': 'United Kingdom',
              'airports': [],         'rail': 'GBSHF'},
    'GBNOT': {'name': 'Nottingham',   'country': 'United Kingdom',
              'airports': ['EMA'],    'rail': 'GBNOT'},
    # Scotland
    'GBABZ': {'name': 'Aberdeen',     'country': 'United Kingdom',
              'airports': ['ABZ'],    'rail': 'GBABZ'},
    # ── France ──────────────────────────────────────────────────────────────
    'FRPAR': {'name': 'Paris',      'country': 'France',
              'airports': ['CDG', 'ORY'], 'rail': 'FRPAR'},
    'FRLYS': {'name': 'Lyon',       'country': 'France',
              'airports': ['LYS'],  'rail': 'FRLYS'},
    'FRMRS': {'name': 'Marseille',  'country': 'France',
              'airports': ['MRS'],  'rail': 'FRMRS'},
    'FRNIC': {'name': 'Nice',       'country': 'France',
              'airports': ['NCE'],  'rail': 'FRNIC'},
    'FRBOD': {'name': 'Bordeaux',   'country': 'France',
              'airports': ['BOD'],  'rail': 'FRBOD'},
    'FRTLS': {'name': 'Toulouse',   'country': 'France',
              'airports': ['TLS'],  'rail': 'FRTLS'},
    'FRSXB': {'name': 'Strasbourg', 'country': 'France',
              'airports': ['SXB'],  'rail': 'FRSXB'},
    'FRNTE': {'name': 'Nantes',     'country': 'France',
              'airports': ['NTE'],  'rail': 'FRNTE'},
    'FRLIL': {'name': 'Lille',      'country': 'France',
              'airports': ['LIL'],  'rail': 'FRLIL'},
    'FRMPL': {'name': 'Montpellier','country': 'France',
              'airports': ['MPL'],  'rail': 'FRMPL'},
    'FRRNS': {'name': 'Rennes',     'country': 'France',
              'airports': ['RNS'],  'rail': 'FRRNS'},
    # ── Belgium / Netherlands ───────────────────────────────────────────────
    'BEBRU': {'name': 'Brussels',   'country': 'Belgium',
              'airports': ['BRU'],  'rail': 'BEBRU'},
    'NLAMS': {'name': 'Amsterdam',  'country': 'Netherlands',
              'airports': ['AMS'],  'rail': 'NLAMS'},
    'NLRTM': {'name': 'Rotterdam',  'country': 'Netherlands',
              'airports': ['RTM'],  'rail': 'NLRTM'},
    'BEANR': {'name': 'Antwerp',    'country': 'Belgium',
              'airports': ['ANR'],  'rail': 'BEANR'},
    # ── Luxembourg ──────────────────────────────────────────────────────────
    'LULUX': {'name': 'Luxembourg', 'country': 'Luxembourg',
              'airports': ['LUX'],  'rail': 'LULUX'},
    # ── Germany ─────────────────────────────────────────────────────────────
    'DEFRA': {'name': 'Frankfurt',  'country': 'Germany',
              'airports': ['FRA'],  'rail': 'DEFRA'},
    'DEBER': {'name': 'Berlin',     'country': 'Germany',
              'airports': ['BER', 'TXL'], 'rail': 'DEBER'},
    'DEMUC': {'name': 'Munich',     'country': 'Germany',
              'airports': ['MUC'],  'rail': 'DEMUC'},
    'DEHAM': {'name': 'Hamburg',    'country': 'Germany',
              'airports': ['HAM'],  'rail': 'DEHAM'},
    'DECGN': {'name': 'Cologne',    'country': 'Germany',
              'airports': ['CGN'],  'rail': 'DECGN'},
    'DESTT': {'name': 'Stuttgart',  'country': 'Germany',
              'airports': ['STR'],  'rail': 'DESTT'},
    'DEDUS': {'name': 'Düsseldorf', 'country': 'Germany',
              'airports': ['DUS'],  'rail': 'DEDUS'},
    'DENUR': {'name': 'Nuremberg',  'country': 'Germany',
              'airports': ['NUE'],  'rail': 'DENUR'},
    'DEHAN': {'name': 'Hannover',   'country': 'Germany',
              'airports': ['HAJ'],  'rail': 'DEHAN'},
    'DELEI': {'name': 'Leipzig',    'country': 'Germany',
              'airports': ['LEJ'],  'rail': 'DELEI'},
    'DEDRS': {'name': 'Dresden',    'country': 'Germany',
              'airports': ['DRS'],  'rail': 'DEDRS'},
    # ── Switzerland ─────────────────────────────────────────────────────────
    'CHZRH': {'name': 'Zurich',     'country': 'Switzerland',
              'airports': ['ZRH'],  'rail': 'CHZRH'},
    'CHGVA': {'name': 'Geneva',     'country': 'Switzerland',
              'airports': ['GVA'],  'rail': 'CHGVA'},
    'CHBSL': {'name': 'Basel',      'country': 'Switzerland',
              'airports': ['BSL', 'EAP', 'MLH'], 'rail': 'CHBSL'},
    'CHBRN': {'name': 'Bern',       'country': 'Switzerland',
              'airports': ['BRN'],  'rail': 'CHBRN'},
    'CHLAS': {'name': 'Lausanne',   'country': 'Switzerland',
              'airports': [],       'rail': 'CHLAS'},
    # ── Austria ─────────────────────────────────────────────────────────────
    'ATVIE': {'name': 'Vienna',     'country': 'Austria',
              'airports': ['VIE'],  'rail': 'ATVIE'},
    'ATSBG': {'name': 'Salzburg',   'country': 'Austria',
              'airports': ['SZG'],  'rail': 'ATSBG'},
    'ATGRZ': {'name': 'Graz',       'country': 'Austria',
              'airports': ['GRZ'],  'rail': 'ATGRZ'},
    'ATINN': {'name': 'Innsbruck',  'country': 'Austria',
              'airports': ['INN'],  'rail': 'ATINN'},
    # ── Italy ───────────────────────────────────────────────────────────────
    'ITMIL': {'name': 'Milan',      'country': 'Italy',
              'airports': ['MXP', 'LIN'], 'rail': 'ITMIL'},
    'ITROM': {'name': 'Rome',       'country': 'Italy',
              'airports': ['FCO', 'CIA'], 'rail': 'ITROM'},
    'ITTRN': {'name': 'Turin',      'country': 'Italy',
              'airports': ['TRN'],  'rail': 'ITTRN'},
    'ITFLO': {'name': 'Florence',   'country': 'Italy',
              'airports': ['FLR'],  'rail': 'ITFLO'},
    'ITVCE': {'name': 'Venice',     'country': 'Italy',
              'airports': ['VCE', 'TSF'], 'rail': 'ITVCE'},
    'ITNAP': {'name': 'Naples',     'country': 'Italy',
              'airports': ['NAP'],  'rail': 'ITNAP'},
    'ITBLN': {'name': 'Bologna',    'country': 'Italy',
              'airports': ['BLQ'],  'rail': 'ITBLN'},
    'ITGOA': {'name': 'Genoa',      'country': 'Italy',
              'airports': ['GOA'],  'rail': 'ITGOA'},
    'ITVRS': {'name': 'Verona',     'country': 'Italy',
              'airports': ['VRN'],  'rail': 'ITVRS'},
    # ── Monaco ──────────────────────────────────────────────────────────────
    'MCMON': {'name': 'Monaco',     'country': 'Monaco',
              'airports': [],       'rail': 'MCMON'},
    # ── Spain / Portugal ────────────────────────────────────────────────────
    'ESMAD': {'name': 'Madrid',     'country': 'Spain',
              'airports': ['MAD'],  'rail': 'ESMAD'},
    'ESBCN': {'name': 'Barcelona',  'country': 'Spain',
              'airports': ['BCN'],  'rail': 'ESBCN'},
    'ESSVQ': {'name': 'Seville',    'country': 'Spain',
              'airports': ['SVQ'],  'rail': 'ESSVQ'},
    'ESVLC': {'name': 'Valencia',   'country': 'Spain',
              'airports': ['VLC'],  'rail': 'ESVLC'},
    'ESMLG': {'name': 'Málaga',     'country': 'Spain',
              'airports': ['AGP'],  'rail': 'ESMLG'},
    'PTLIS': {'name': 'Lisbon',     'country': 'Portugal',
              'airports': ['LIS'],  'rail': 'PTLIS'},
    'PTOPO': {'name': 'Porto',      'country': 'Portugal',
              'airports': ['OPO'],  'rail': 'PTOPO'},
    # ── Czech Republic / Slovakia ────────────────────────────────────────────
    'CZPRG': {'name': 'Prague',     'country': 'Czech Republic',
              'airports': ['PRG'],  'rail': 'CZPRG'},
    'CZBRQ': {'name': 'Brno',       'country': 'Czech Republic',
              'airports': ['BRQ'],  'rail': 'CZBRQ'},
    'SKBTS': {'name': 'Bratislava', 'country': 'Slovakia',
              'airports': ['BTS'],  'rail': 'SKBTS'},
    # ── Hungary / Romania ────────────────────────────────────────────────────
    'HUBUD': {'name': 'Budapest',   'country': 'Hungary',
              'airports': ['BUD'],  'rail': 'HUBUD'},
    'ROBUH': {'name': 'Bucharest',  'country': 'Romania',
              'airports': ['OTP', 'BBU'], 'rail': 'ROBUH'},
    # ── Poland ──────────────────────────────────────────────────────────────
    'PLWAW': {'name': 'Warsaw',     'country': 'Poland',
              'airports': ['WAW'],  'rail': 'PLWAW'},
    'PLKRK': {'name': 'Kraków',     'country': 'Poland',
              'airports': ['KRK'],  'rail': 'PLKRK'},
    'PLWRO': {'name': 'Wrocław',    'country': 'Poland',
              'airports': ['WRO'],  'rail': 'PLWRO'},
    'PLGDN': {'name': 'Gdańsk',     'country': 'Poland',
              'airports': ['GDN'],  'rail': 'PLGDN'},
    # ── Western Balkans / Southeast Europe ──────────────────────────────────
    'SILJB': {'name': 'Ljubljana',   'country': 'Slovenia',
              'airports': ['LJU'],  'rail': 'SILJB'},
    'HRZAG': {'name': 'Zagreb',      'country': 'Croatia',
              'airports': ['ZAG'],  'rail': 'HRZAG'},
    'RSBEG': {'name': 'Belgrade',    'country': 'Serbia',
              'airports': ['BEG'],  'rail': 'RSBEG'},
    'BGSFP': {'name': 'Sofia',       'country': 'Bulgaria',
              'airports': ['SOF'],  'rail': 'BGSFP'},
    'GRTHE': {'name': 'Thessaloniki','country': 'Greece',
              'airports': ['SKG'],  'rail': 'GRTHE'},
    'GRATH': {'name': 'Athens',      'country': 'Greece',
              'airports': ['ATH'],  'rail': 'GRATH'},
    'TRIST': {'name': 'Istanbul',    'country': 'Turkey',
              'airports': ['IST', 'SAW'], 'rail': 'TRIST'},
    # ── Scandinavia ─────────────────────────────────────────────────────────
    'SESTO': {'name': 'Stockholm',  'country': 'Sweden',
              'airports': ['ARN', 'NYO'], 'rail': 'SESTO'},
    'SEGOT': {'name': 'Gothenburg', 'country': 'Sweden',
              'airports': ['GOT'],  'rail': 'SEGOT'},
    'SEMAL': {'name': 'Malmö',      'country': 'Sweden',
              'airports': ['MMX'],  'rail': 'SEMAL'},
    'DKCPH': {'name': 'Copenhagen', 'country': 'Denmark',
              'airports': ['CPH'],  'rail': 'DKCPH'},
    'NOOSL': {'name': 'Oslo',       'country': 'Norway',
              'airports': ['OSL'],  'rail': 'NOOSL'},
    # ── Baltic ──────────────────────────────────────────────────────────────
    'EETAL': {'name': 'Tallinn',    'country': 'Estonia',
              'airports': ['TLL'],  'rail': 'EETAL'},
    'LVRIX': {'name': 'Riga',       'country': 'Latvia',
              'airports': ['RIX'],  'rail': 'LVRIX'},
    'LTVNO': {'name': 'Vilnius',    'country': 'Lithuania',
              'airports': ['VNO'],  'rail': 'LTVNO'},
}

# Reverse lookups derived from CITIES at startup
IATA_TO_CITY    = {}                    # IATA  → city_code
STATION_TO_IATAS = defaultdict(list)   # rail station → [airport IATAs]
for _city_code, _cinfo in CITIES.items():
    for _iata in _cinfo['airports']:
        IATA_TO_CITY.setdefault(_iata, _city_code)  # first CITIES entry wins (primary city)
    if _cinfo['rail']:
        STATION_TO_IATAS[_cinfo['rail']].extend(_cinfo['airports'])

# High-speed and main-line rail connections (bidirectional, distances in km).
# Covers the Interrail network across the UK and Europe.
_RAIL_EDGES = [
    # ── UK internal ──────────────────────────────────────────────────────────
    # ECML (East Coast Main Line): London → Leeds → Newcastle → Edinburgh
    ('GBLON', 'GBLED',  310, 'LNER'),
    ('GBLON', 'GBNEW',  445, 'LNER'),
    ('GBLON', 'GBEDB',  630, 'LNER'),
    ('GBLED', 'GBNEW',  150, 'LNER'),
    ('GBNEW', 'GBEDB',  190, 'LNER'),
    ('GBLED', 'GBEDB',  310, 'LNER'),
    # WCML (West Coast Main Line): London → Birmingham → Manchester / Liverpool → Glasgow
    ('GBLON', 'GBBHM',  180, 'Avanti West Coast'),
    ('GBLON', 'GBMAN',  295, 'Avanti West Coast'),
    ('GBLON', 'GBLIV',  320, 'Avanti West Coast'),
    ('GBLON', 'GBGLA',  645, 'Avanti West Coast'),
    ('GBEDB', 'GBGLA',   75, 'ScotRail'),
    # Cross-country / inter-city
    ('GBBHM', 'GBMAN',  130, 'Avanti/CrossCountry'),
    ('GBBHM', 'GBLIV',  130, 'Avanti/CrossCountry'),
    ('GBBHM', 'GBEDB',  450, 'CrossCountry'),
    ('GBMAN', 'GBLED',   70, 'TransPennine'),
    ('GBMAN', 'GBLIV',   56, 'Northern/Avanti'),
    ('GBMAN', 'GBNEW',  230, 'TransPennine'),
    ('GBMAN', 'GBGLA',  345, 'Avanti West Coast'),
    ('GBMAN', 'GBEDB',  335, 'TransPennine/LNER'),
    # GWR (Great Western Main Line): London → Bristol
    ('GBLON', 'GBBRS',  190, 'GWR'),
    # South Wales: London → Cardiff ↔ Bristol
    ('GBLON', 'GBCDF',  250, 'GWR'),
    ('GBBHM', 'GBCDF',  170, 'CrossCountry'),
    ('GBBRS', 'GBCDF',   84, 'GWR'),
    # South Western Main Line: London → Southampton
    ('GBLON', 'GBSOU',  125, 'South Western Railway'),
    # Midland Main Line: London → Nottingham → Sheffield
    ('GBLON', 'GBSHF',  257, 'East Midlands Railway'),
    ('GBLON', 'GBNOT',  206, 'East Midlands Railway'),
    ('GBNOT', 'GBSHF',   56, 'East Midlands Railway'),
    ('GBSHF', 'GBLED',   48, 'TransPennine Express'),
    ('GBSHF', 'GBMAN',   58, 'TransPennine Express'),
    ('GBSHF', 'GBBHM',  130, 'CrossCountry'),
    # ScotRail: Edinburgh → Aberdeen
    ('GBEDB', 'GBABZ',  240, 'ScotRail'),
    # LNER: London → Aberdeen
    ('GBLON', 'GBABZ',  850, 'LNER'),
    # ── Channel Tunnel ───────────────────────────────────────────────────────
    ('GBLON', 'FRPAR',  493, 'Eurostar'),
    ('GBLON', 'BEBRU',  370, 'Eurostar'),
    # ── France internal (TGV) ────────────────────────────────────────────────
    ('FRPAR', 'FRLYS',  465, 'TGV'),
    ('FRPAR', 'FRMRS',  772, 'TGV'),
    ('FRPAR', 'FRNIC',  930, 'TGV'),
    ('FRPAR', 'FRBOD',  580, 'TGV'),
    ('FRPAR', 'FRTLS',  680, 'TGV'),
    ('FRPAR', 'FRNTE',  385, 'TGV'),
    ('FRPAR', 'FRSXB',  490, 'TGV'),
    ('FRLYS', 'FRMRS',  315, 'TGV'),
    ('FRLYS', 'FRTLS',  530, 'TGV'),
    ('FRLYS', 'CHGVA',  155, 'TGV/IC'),
    ('FRLYS', 'ITMIL',  400, 'TGV/Frecciarossa'),
    ('FRMRS', 'FRNIC',  200, 'TGV'),
    # Lille — Eurostar/TGV hub
    ('GBLON', 'FRLIL',  350, 'Eurostar'),
    ('FRPAR', 'FRLIL',  220, 'TGV'),
    ('BEBRU', 'FRLIL',  115, 'Eurostar/Thalys'),
    # Montpellier
    ('FRPAR', 'FRMPL',  750, 'TGV'),
    ('FRMRS', 'FRMPL',  165, 'TGV'),
    ('FRTLS', 'FRMPL',  240, 'TGV'),
    # Rennes
    ('FRPAR', 'FRRNS',  335, 'TGV'),
    ('FRNTE', 'FRRNS',  110, 'TGV/Intercités'),
    # ── France / Benelux / Germany ───────────────────────────────────────────
    ('FRPAR', 'BEBRU',  312, 'Eurostar/Thalys'),
    ('FRPAR', 'NLAMS',  503, 'Thalys'),
    ('FRPAR', 'DEFRA',  579, 'TGV/ICE'),
    ('FRPAR', 'CHZRH',  493, 'TGV'),
    ('FRPAR', 'CHGVA',  501, 'TGV'),
    ('FRPAR', 'ESBCN', 1040, 'TGV/AVE'),
    ('FRPAR', 'ITMIL',  637, 'TGV/Frecciarossa'),
    ('FRPAR', 'ITTRN',  660, 'TGV/Frecciarossa'),
    # Luxembourg
    ('FRPAR', 'LULUX',  340, 'TGV/IC'),
    ('BEBRU', 'LULUX',  215, 'IC'),
    ('DEFRA', 'LULUX',  200, 'IC'),
    ('FRSXB', 'DEFRA',  220, 'TGV/ICE'),
    ('FRSXB', 'CHBSL',   80, 'TER/IC'),
    ('BEBRU', 'NLAMS',  192, 'Thalys'),
    ('BEBRU', 'DEFRA',  496, 'ICE/Thalys'),
    ('BEBRU', 'DECGN',  220, 'Thalys'),
    # Antwerp
    ('BEBRU', 'BEANR',   45, 'IC'),
    ('NLAMS', 'BEANR',  160, 'IC'),
    ('NLRTM', 'BEANR',  120, 'IC'),
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
    ('DEMUC', 'DEBER',  623, 'ICE'),
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
    ('CHGVA', 'CHLAS',   65, 'IC'),
    ('CHLAS', 'CHBRN',  105, 'IC'),
    ('CHLAS', 'CHZRH',  228, 'IC'),
    ('FRPAR', 'CHLAS',  490, 'TGV'),
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
    # Innsbruck — Brenner corridor
    ('DEMUC', 'ATINN',  165, 'Railjet'),
    ('ATINN', 'ITVRS',  210, 'Railjet'),
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
    ('ITVCE', 'ATVIE',  580, 'Nightjet/Railjet'),
    # Genoa
    ('ITMIL', 'ITGOA',  145, 'Frecciabianca'),
    ('ITTRN', 'ITGOA',  170, 'Trenitalia'),
    # Verona
    ('ITMIL', 'ITVRS',  157, 'Frecciarossa'),
    ('ITVCE', 'ITVRS',  115, 'Frecciarossa'),
    # ── Monaco (terminal — reached only from Nice via coastal TER) ───────────
    ('FRNIC', 'MCMON',   20, 'TER'),
    # ── Iberia (AVE / Alfa Pendular) ─────────────────────────────────────────
    ('ESBCN', 'ESMAD',  620, 'AVE'),
    ('ESBCN', 'ESVLC',  350, 'AVE'),
    ('ESMAD', 'ESVLC',  390, 'AVE'),
    ('ESMAD', 'ESSVQ',  470, 'AVE'),
    ('ESMAD', 'ESMLG',  530, 'AVE'),
    # No international rail to Portugal (Madrid–Lisbon Talgo suspended 2020, AVE not yet built)
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
    # Leipzig
    ('DEBER', 'DELEI',  190, 'ICE'),
    ('DEFRA', 'DELEI',  310, 'ICE'),
    ('DENUR', 'DELEI',  270, 'ICE'),
    # Dresden
    ('DEBER', 'DEDRS',  200, 'ICE'),
    ('CZPRG', 'DEDRS',  150, 'EC'),
    # ── Southeast Europe / Balkans ───────────────────────────────────────────
    ('RSBEG', 'BGSFP',  400, 'EC'),
    ('BGSFP', 'GRTHE',  565, 'IC'),
    ('BGSFP', 'ROBUH',  390, 'IC'),
    ('BGSFP', 'TRIST',  560, 'EC'),
    ('GRTHE', 'GRATH',  500, 'IC'),
    # Thessaloniki–Istanbul rail link non-operational (track condition/border closure)
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
    """
    Search for cities by name.  CITIES (rail-capable European cities) are
    checked first so that rail-only cities (Sheffield, Oxford …) surface even
    though they have no entry in the airports CSV.  Non-European cities fall
    back to the AIRPORTS dict as before.

    Every result carries a 'rail' field (station code or None) and a
    'city_code' field (CITIES key or None) so callers can persist both the
    airports and the rail station for an attendee.
    """
    import unicodedata
    def _norm(s):
        """Lowercase + strip diacritics so 'Zürich'/'zurich'/'Zurich' all match."""
        return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode().lower()

    q     = _norm(query.strip())
    results = []
    seen_locations = set()

    # ── 1. Search CITIES (European rail network) ─────────────────────────
    for city_code, cinfo in CITIES.items():
        if (q not in _norm(cinfo['name'])
                and q not in _norm(cinfo['country'])
                and q != city_code.lower()):
            continue
        routable = [i for i in cinfo['airports'] if i in GRAPH and i in MAIN_AIRPORTS]
        has_rail  = bool(cinfo['rail'] and cinfo['rail'] in RAIL_GRAPH)
        if not routable and not has_rail:
            continue
        location = f"{cinfo['name']}, {cinfo['country']}"
        if location in seen_locations:
            continue
        seen_locations.add(location)
        continent = 'Europe'
        if routable:
            continent = AIRPORTS.get(routable[0], {}).get('continent', 'Europe')
        results.append({
            'location':  location,
            'city_code': city_code,
            'iatas':     routable,
            'rail':      cinfo['rail'] if has_rail else None,
            'continent': continent,
            'airports':  [{'iata': i, 'name': AIRPORTS.get(i, {}).get('name', i),
                           'continent': AIRPORTS.get(i, {}).get('continent', 'Europe')}
                          for i in routable],
        })

    # ── 2. Fall back to raw AIRPORTS for non-CITIES matches ──────────────
    matches = []
    for iata, info in AIRPORTS.items():
        if q in _norm(info['city']) or q in _norm(info['country']) or q == iata.lower():
            matches.append((iata, info))
    matches.sort(key=lambda x: (0 if _norm(x[1]['city']) == q else 1,
                                 x[1]['city'], x[1]['name']))
    groups = {}
    for iata, info in matches:
        if not info['city'].strip():
            continue
        key = f"{info['city']}, {info['country']}"
        groups.setdefault(key, []).append(iata)
    for loc, iatas in groups.items():
        if loc in seen_locations:
            continue   # already covered by CITIES
        routable = [i for i in iatas if i in GRAPH and i in MAIN_AIRPORTS]
        if not routable:
            continue
        seen_locations.add(loc)
        results.append({
            'location':  loc,
            'city_code': None,
            'iatas':     routable,
            'rail':      None,
            'continent': AIRPORTS[routable[0]]['continent'],
            'airports':  [{'iata': i, 'name': AIRPORTS[i]['name'],
                           'continent': AIRPORTS[i]['continent']} for i in routable],
        })

    # Sort: exact name match first, CITIES before non-CITIES
    results.sort(key=lambda r: (
        0 if r['location'].split(',')[0].strip().lower() == q else 1,
        0 if r.get('city_code') else 1,
    ))
    return results[:20]


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


def find_meeting_destinations(attendees, top_n=10, continent_filter=None, nights=0):
    """
    attendees:        list of {'city': str, 'iatas': [str], 'rail': str|None,
                               'count': int}
    continent_filter: if set (e.g. 'Europe'), only airports on that continent
                      are considered as candidate destinations.
    nights:           number of hotel nights to include in cost ranking (0 = transport only).
    Returns (ranked, ranked_home)

    Groups are keyed by (iata_tuple, rail_station) so that rail-only cities
    (empty iatas but a valid rail station) are routed correctly.
    """
    unique_origins = {}
    for a in attendees:
        iata_key = tuple(sorted(a.get('iatas', [])))
        rail_key  = a.get('rail')
        key = (iata_key, rail_key)
        unique_origins.setdefault(key, []).append((a['city'], a['count']))

    # ── Air distance maps ─────────────────────────────────────────────────
    dist_maps = {}
    for (iata_tuple, rail_station), city_list in unique_origins.items():
        merged = {}
        for origin_iata in iata_tuple:
            if origin_iata not in GRAPH:
                continue
            result = dijkstra_all(origin_iata)
            for dest, (h, d) in result.items():
                cur = merged.get(dest, (math.inf, math.inf, None))
                if h < cur[0] or (h == cur[0] and d < cur[1]):
                    merged[dest] = (h, d, origin_iata)
        dist_maps[(iata_tuple, rail_station)] = merged

    # ── Rail distance maps ────────────────────────────────────────────────
    # Uses the rail station attached to each attendee group directly —
    # no airport-to-rail lookup needed.
    # raw_rail_maps: station → (hops, dist) — kept for scoring rail-only home cities
    rail_dist_maps = {}
    raw_rail_maps  = {}
    for (iata_tuple, rail_station), city_list in unique_origins.items():
        merged_rail = {}
        if rail_station and rail_station in RAIL_GRAPH:
            rail_result = dijkstra_rail_all(rail_station)
            raw_rail_maps[(iata_tuple, rail_station)] = rail_result
            for rail_dest_station, (rh, rd) in rail_result.items():
                n_xfr = max(0, rh - 1)
                # Store the station code itself so rail-only cities (e.g. Sheffield,
                # Oxford) enter the candidate pool and can be ranked as destinations.
                cur = merged_rail.get(rail_dest_station, (math.inf, math.inf, math.inf, None))
                if rh < cur[0] or (rh == cur[0] and rd < cur[1]):
                    merged_rail[rail_dest_station] = (rh, rd, n_xfr, rail_station)
                # Also add nearby airports reachable via this rail station.
                for dest_airport in STATION_TO_IATAS.get(rail_dest_station, []):
                    cur = merged_rail.get(dest_airport, (math.inf, math.inf, math.inf, None))
                    if rh < cur[0] or (rh == cur[0] and rd < cur[1]):
                        merged_rail[dest_airport] = (rh, rd, n_xfr, rail_station)
        else:
            raw_rail_maps[(iata_tuple, rail_station)] = {}
        rail_dist_maps[(iata_tuple, rail_station)] = merged_rail

    # ── Hybrid cost maps (rail-only groups: train to a hub, then fly) ─────
    # For rail-only attendees (no airports) compute the cheapest
    # rail-to-hub + air option for every destination.  Prevents unrealistic
    # full-rail routes for long hauls (e.g. York → Barcelona).
    _hub_air_cache: dict = {}   # hub_airport → dijkstra_all result
    hybrid_cost_maps: dict = {}
    for (iata_tuple, rail_station), city_list in unique_origins.items():
        if iata_tuple or not rail_station or rail_station not in RAIL_GRAPH:
            hybrid_cost_maps[(iata_tuple, rail_station)] = {}
            continue

        rail_result = raw_rail_maps.get((iata_tuple, rail_station), {})
        best_hybrid: dict = {}  # dest_iata → (price, carbon, hops, dist)

        for hub_station, (rh, rd) in rail_result.items():
            if hub_station == rail_station:
                continue
            hub_airports = STATION_TO_IATAS.get(hub_station, [])
            if not hub_airports:
                continue
            n_rail_xfr = max(0, rh - 1)
            rail_p, rail_c = estimate_rail_fare(rd, n_rail_xfr, rail_station, hub_station)

            for hub_airport in hub_airports:
                if hub_airport not in GRAPH:
                    continue
                if hub_airport not in _hub_air_cache:
                    _hub_air_cache[hub_airport] = dijkstra_all(hub_airport)
                for dest_iata, (ah, ad) in _hub_air_cache[hub_airport].items():
                    air_p, air_c = estimate_fare(ad, ah, hub_airport, dest_iata)
                    total_p = rail_p + air_p
                    cur = best_hybrid.get(dest_iata)
                    if cur is None or total_p < cur[0]:
                        best_hybrid[dest_iata] = (total_p, rail_c + air_c, rh + ah, rd + ad)

        hybrid_cost_maps[(iata_tuple, rail_station)] = best_hybrid

    # ── Gateway cost maps (air-origin groups: fly to a hub, then train) ───
    # Mirror of hybrid for attendees who HAVE airports but are travelling to a
    # rail destination.  For every reachable rail station we keep the cheapest
    # fly-to-hub + train-to-station option.  Without this the destination
    # ranking would score air-origin → rail-only cities (e.g. Munich →
    # Sheffield) as an unrealistic continent-crossing all-rail trip, while
    # get_routes_for_destination correctly flies to a hub then trains in —
    # producing wildly different carbon numbers between the table and drilldown.
    _station_rail_cache: dict = {}   # hub_station → dijkstra_rail_all result
    gateway_cost_maps: dict = {}     # key → {dest_station: (price, carbon, hops, dist)}
    for (iata_tuple, rail_station), city_list in unique_origins.items():
        if not iata_tuple:
            gateway_cost_maps[(iata_tuple, rail_station)] = {}
            continue
        air_map = dist_maps[(iata_tuple, rail_station)]
        best_gw: dict = {}
        for hub_station, hub_airports in STATION_TO_IATAS.items():
            # Cheapest flight from this group's origins to the hub airport
            fly_p = fly_c = fly_h = fly_d = None
            for hub_airport in hub_airports:
                ac = air_map.get(hub_airport)
                if ac is None:
                    continue
                ah, ad, aorigin = ac
                a_p, a_c = estimate_fare(ad, ah, aorigin, hub_airport)
                if fly_p is None or a_p < fly_p:
                    fly_p, fly_c, fly_h, fly_d = a_p, a_c, ah, ad
            if fly_p is None:
                continue
            if hub_station not in _station_rail_cache:
                _station_rail_cache[hub_station] = dijkstra_rail_all(hub_station)
            for dest_station, (rh, rd) in _station_rail_cache[hub_station].items():
                if rd == 0:
                    continue   # same station — that's just the flight (air option)
                r_p, r_c = estimate_rail_fare(rd, max(0, rh - 1), hub_station, dest_station)
                total_p = fly_p + r_p
                cur = best_gw.get(dest_station)
                if cur is None or total_p < cur[0]:
                    best_gw[dest_station] = (total_p, fly_c + r_c, fly_h + rh, fly_d + rd)
        gateway_cost_maps[(iata_tuple, rail_station)] = best_gw

    all_origin_iatas = set(i for (iata_tuple, _) in unique_origins for i in iata_tuple)

    if not unique_origins:
        return [], {}

    # Candidate pool = intersection of each group's reachable destinations
    # (air + rail + hybrid + gateway combined).  Gateway is what lets an
    # air-only origin (no rail) reach a rail-only destination (no airport) —
    # without it such cities (e.g. Sheffield from New York) would never appear
    # in the ranking even though their route drilldown works.
    all_reachable = {}
    for key in unique_origins:
        all_reachable[key] = (
            set(dist_maps[key].keys()) |
            set(rail_dist_maps[key].keys()) |
            set(hybrid_cost_maps.get(key, {}).keys()) |
            set(gateway_cost_maps.get(key, {}).keys())
        )

    reachable_values = list(all_reachable.values())
    if not reachable_values or not any(reachable_values):
        return [], {}
    candidate_pool = reachable_values[0]
    for v in reachable_values[1:]:
        candidate_pool = candidate_pool & v

    # Add home airports as candidates only if they match the continent filter
    # (or if there is no filter). This means home cities in a different continent
    # won't appear in the top 10 when a continent is selected.
    def _dest_continent(dest):
        """Continent string for any candidate (airport IATA or rail station code)."""
        if dest in AIRPORTS:
            return AIRPORTS[dest].get('continent')
        if dest in CITIES:   # rail station code — all CITIES are European
            return 'Europe'
        return None

    if continent_filter and continent_filter != 'Any':
        matching_home_iatas = {
            iata for iata in all_origin_iatas
            if AIRPORTS.get(iata, {}).get('continent') == continent_filter
        }
        candidate_pool = {
            dest for dest in candidate_pool
            if _dest_continent(dest) == continent_filter
        } | matching_home_iatas
        log.info("Continent filter '%s': %d candidates (incl. %d home airports on that continent)",
                 continent_filter, len(candidate_pool), len(matching_home_iatas))
    else:
        candidate_pool |= all_origin_iatas

    # ── Canonical lookup helpers ─────────────────────────────────────────────
    # The cost maps are keyed inconsistently:
    #   • dist_maps / hybrid_cost_maps  → destination AIRPORT IATA
    #   • rail_dist_maps / gateway_cost_maps → destination RAIL STATION code
    # A candidate `dest` may itself be a CITIES city-code (e.g. 'DEMUC'), a raw
    # airport IATA ('JFK'), or an airport belonging to a known city.  Resolving
    # every candidate to its canonical (airports, rail_station) up front — and
    # querying each map with the right keys — removes the recurring class of
    # "forgotten fallback" bugs (Munich, London, Sheffield…).
    def _dest_airports_and_rail(dest):
        if dest in CITIES:
            return CITIES[dest]['airports'], CITIES[dest].get('rail')
        cc = IATA_TO_CITY.get(dest)
        if cc and cc in CITIES:
            return CITIES[cc]['airports'], CITIES[cc].get('rail')
        return [dest], None

    def _best_air_cost(amap, airports):
        """Fewest-hops-then-shortest air entry across a city's airports.
        Returns ((hops, dist, origin_iata), landed_iata) or (None, None)."""
        best = best_apt = None
        for apt in airports:
            c = amap.get(apt)
            if c is None:
                continue
            if (best is None or c[0] < best[0]
                    or (c[0] == best[0] and c[1] < best[1])):
                best, best_apt = c, apt
        return best, best_apt

    def _best_hybrid_cost(hmap, airports):
        """Cheapest hybrid (train→hub→fly) entry across a city's airports."""
        best = None
        for apt in airports:
            c = hmap.get(apt)
            if c is not None and (best is None or c[0] < best[0]):
                best = c
        return best

    total_attendees  = sum(a['count'] for a in attendees)

    def _score_destination(dest):
        """Total (hops, dist, price, carbon) for every attendee travelling to
        `dest`, using the canonical per-mode cost lookups and mode-selection
        rule.  Returns None if any group cannot reach the destination.

        This is the SINGLE source of truth for destination scoring — both the
        overall ranking and the per-attendee home-city ranking call it, so the
        two tables can never disagree (the recurring Sheffield/Munich carbon
        mismatch came from a second, divergent home-city implementation)."""
        total_hops  = 0
        total_dist  = 0.0
        total_price = 0
        total_carbon= 0.0
        for (iata_tuple, rail_station), city_list in unique_origins.items():
            total_count = sum(c for _, c in city_list)

            # ── Canonical destination keys ──────────────────────────────────
            # Resolve the candidate to its city's airports + rail station once,
            # then query every cost map with the matching key type.
            _dest_airports_scoring, _dest_rail_scoring = _dest_airports_and_rail(dest)
            is_home_scoring = (
                bool(set(iata_tuple) & set(_dest_airports_scoring)) or
                (not iata_tuple and _dest_rail_scoring and _dest_rail_scoring == rail_station)
            )
            if is_home_scoring:
                pass  # home city — zero cost, zero distance
            else:
                # ── Air cost (maps keyed by destination airport IATA) ───────
                air_cost, _air_dest_iata = _best_air_cost(
                    dist_maps[(iata_tuple, rail_station)], _dest_airports_scoring)

                # ── Rail cost (maps keyed by destination station code) ──────
                _rail_map = rail_dist_maps[(iata_tuple, rail_station)]
                rail_cost = (
                    _rail_map.get(_dest_rail_scoring) if _dest_rail_scoring
                    else _rail_map.get(dest)   # raw airport with a rail alias
                )

                # ── Hybrid: train → hub → fly (keyed by dest airport IATA) ──
                hybrid_cost = _best_hybrid_cost(
                    hybrid_cost_maps.get((iata_tuple, rail_station), {}),
                    _dest_airports_scoring)

                # ── Gateway: fly → hub → train (keyed by dest station code) ─
                gateway_cost = (
                    gateway_cost_maps.get((iata_tuple, rail_station), {}).get(_dest_rail_scoring)
                    if _dest_rail_scoring else None
                )

                if (air_cost is None and rail_cost is None
                        and hybrid_cost is None and gateway_cost is None):
                    return None

                # ── Per-mode prices ──────────────────────────────────────────
                if air_cost is not None:
                    a_hops, a_dist, best_origin = air_cost
                    a_price, a_carbon = estimate_fare(a_dist, a_hops, best_origin, _air_dest_iata)
                else:
                    a_hops = a_dist = a_price = a_carbon = math.inf

                if rail_cost:
                    rh, rd, n_xfr, _ = rail_cost
                    r_price, r_carbon = estimate_rail_fare(rd, n_xfr, rail_station, _dest_rail_scoring)
                else:
                    rh = rd = r_price = r_carbon = math.inf

                hyb_price = hybrid_cost[0] if hybrid_cost is not None else math.inf
                gw_price  = gateway_cost[0] if gateway_cost is not None else math.inf

                # ── Decide mode — mirror get_routes_for_destination exactly ──
                # so the table totals match the route drilldown.
                _SHORT_RAIL_KM = 300

                # Best single-mode option (air vs rail).
                single = None          # 'air' | 'rail'
                s_hops = s_dist = math.inf
                if air_cost is not None or rail_cost:
                    if air_cost is not None and rail_cost:
                        if rh < a_hops:
                            pick_rail = True
                        elif rh > a_hops:
                            pick_rail = rd <= _SHORT_RAIL_KM
                        else:
                            pick_rail = r_price <= a_price * 1.30
                        single = 'rail' if pick_rail else 'air'
                    elif rail_cost:
                        single = 'rail'
                    else:
                        single = 'air'
                    s_hops, s_dist = (rh, rd) if single == 'rail' else (a_hops, a_dist)

                # Best mixed option (hybrid vs gateway), cheaper wins.
                mixed = None           # 'hybrid' | 'gateway'
                m_hops = math.inf
                if hybrid_cost is not None or gateway_cost is not None:
                    if hybrid_cost is not None and (gateway_cost is None
                                                    or hyb_price <= gw_price):
                        mixed, m_hops = 'hybrid', hybrid_cost[2]
                    else:
                        mixed, m_hops = 'gateway', gateway_cost[2]

                # Choose: a short pure-rail trip (≤ 300 km) always wins; otherwise
                # fewest hops wins even across single vs mixed; ties keep the
                # clean single-mode trip.
                if single is not None and mixed is not None:
                    if single == 'rail' and s_dist <= _SHORT_RAIL_KM:
                        choice = single
                    elif m_hops < s_hops:
                        choice = mixed
                    else:
                        choice = single
                elif single is not None:
                    choice = single
                else:
                    choice = mixed

                if choice == 'air':
                    h, d, oneway_price, oneway_carbon = a_hops, a_dist, a_price, a_carbon
                elif choice == 'rail':
                    h, d, oneway_price, oneway_carbon = rh, rd, r_price, r_carbon
                elif choice == 'hybrid':
                    h, d, oneway_price, oneway_carbon = (
                        hybrid_cost[2], hybrid_cost[3], hybrid_cost[0], hybrid_cost[1])
                else:  # gateway
                    h, d, oneway_price, oneway_carbon = (
                        gateway_cost[2], gateway_cost[3], gateway_cost[0], gateway_cost[1])

                total_hops  += h * total_count
                total_dist  += d * total_count
                total_price  += oneway_price  * 2 * total_count
                total_carbon += oneway_carbon * 2 * total_count
        return (total_hops, total_dist, total_price, total_carbon)

    candidate_scores = {}
    for dest in candidate_pool:
        score = _score_destination(dest)
        if score is not None:
            candidate_scores[dest] = score

    # ── Normalise to city_codes ──────────────────────────────────────────────
    # Multiple candidates can represent the same city (e.g. CDG, ORY, and the
    # rail station code FRPAR all map to Paris).  Collapse them into a single
    # city_code entry.
    #
    # Mapping rules:
    #   • airport IATA in IATA_TO_CITY → that city_code  (CDG → FRPAR)
    #   • 5-char CITIES key              → itself          (FRPAR → FRPAR)
    #   • anything else (non-CITIES)     → IATA as-is      (JFK → JFK)
    #
    # IMPORTANT: prefer the canonical CITIES city-code candidate when one exists.
    # That candidate lets every attendee independently pick their best airport
    # for the city (mirroring get_routes_for_destination).  The individual
    # airport candidates (LHR, LGW, STN…) must NOT override it: each of those
    # pins the landing airport for air-origin attendees while still letting
    # rail-origin attendees take the train to the city centre, producing a
    # cheaper-but-inconsistent blend that wouldn't match the route drilldown.
    city_scores: dict = {}
    canonical_cities: set = set()
    # Pass 1 — canonical city-code candidates take priority and are authoritative.
    for dest, score in candidate_scores.items():
        if dest in CITIES:
            city_scores[dest] = score
            canonical_cities.add(dest)
    # Pass 2 — airport / non-CITIES candidates.  Skip any city already covered by
    # a canonical pass-1 candidate (so airport blends can't undercut it).  For
    # cities with no canonical candidate (e.g. air-only reachable, or the home
    # airport when the attendee gave no rail station), compete by lowest cost.
    for dest, score in candidate_scores.items():
        if dest in CITIES:
            continue
        cc = IATA_TO_CITY.get(dest) or dest
        if cc in canonical_cities:
            continue   # canonical candidate is authoritative for this city
        if cc not in city_scores or score[2] < city_scores[cc][2]:
            city_scores[cc] = score

    # ── Add hotel costs to ranking if nights > 0 ─────────────────────────────
    # Uses today's date for the seasonal multiplier (the ranking is approximate
    # anyway; live pricing can be fetched with a specific weeks-ahead date).
    if nights > 0:
        from datetime import date as _date
        _today_str = _date.today().strftime("%Y-%m-%d")
        updated_scores = {}
        for dest, score in city_scores.items():
            total_hops, total_dist, t_price, t_carbon = score
            dest_cc = dest if dest in CITIES else IATA_TO_CITY.get(dest)
            hotel_pp = estimate_hotel_cost(dest_cc, nights, _today_str) if dest_cc else None
            if hotel_pp is not None:
                # Count home attendees for this destination (pay no hotel)
                dest_airports, dest_rail = _dest_airports_and_rail(dest)
                home_count = sum(
                    sum(c for _, c in city_list)
                    for (iata_tuple, rail_station), city_list in unique_origins.items()
                    if (set(iata_tuple) & set(dest_airports)) or
                       (not iata_tuple and dest_rail and dest_rail == rail_station)
                )
                travelling = total_attendees - home_count
                if travelling > 0:
                    t_price += hotel_pp * travelling
            updated_scores[dest] = (total_hops, total_dist, t_price, t_carbon)
        city_scores = updated_scores

    # Sort by lowest cost, then lowest carbon
    all_ranked = sorted(city_scores.items(), key=lambda x: (x[1][2], x[1][3]))

    # City codes for every attendee origin — used for the is_home flag
    all_origin_city_codes: set = set()
    for _a in attendees:
        for _iata in _a.get('iatas', []):
            all_origin_city_codes.add(IATA_TO_CITY.get(_iata, _iata))
        if _a.get('rail'):
            all_origin_city_codes.add(_a['rail'])

    ranked = []
    for city_code, scores in all_ranked:
        # Look up display info from CITIES (preferred) or raw AIRPORTS
        if city_code in CITIES:
            cinfo     = CITIES[city_code]
            _airports = cinfo.get('airports', [])
            continent = (AIRPORTS.get(_airports[0], {}).get('continent', 'Europe')
                         if _airports else 'Europe')
            info = {
                'city':      cinfo['name'],
                'country':   cinfo['country'],
                'name':      cinfo['name'],
                'continent': continent,
            }
        elif city_code in AIRPORTS:
            info = AIRPORTS[city_code]
        else:
            continue   # unknown code — skip

        total_hops, total_dist, total_price, total_carbon = scores
        ranked.append({
            'iata':         city_code,
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
            'is_home':      city_code in all_origin_city_codes,
        })
        if len(ranked) == top_n:
            break

    # ── Home city rankings ────────────────────────────────────────────────
    # A home city is just another candidate destination, so score it with the
    # SAME _score_destination used for the overall ranking.  Sharing one scorer
    # guarantees the "Attendee Home Cities" table can never disagree with the
    # overall table — a previous standalone home-city scorer always used pure
    # rail for rail-capable origins, producing wrong carbon for cities reached
    # by hybrid/gateway in the real routing (e.g. Sheffield).
    home_scores = {}   # home_code -> (display_code, score, local_count)
    for a in attendees:
        # Canonical destination code for this attendee's home city.
        if a.get('rail'):
            home_code = a['rail']
            rail_only = not a.get('iatas')
        elif a.get('iatas'):
            first     = a['iatas'][0]
            home_code = IATA_TO_CITY.get(first, first)
            rail_only = False
        else:
            continue

        score = _score_destination(home_code)
        if score is None:
            continue

        display_code = f'__rail__{home_code}' if rail_only else home_code
        prev         = home_scores.get(home_code)
        # local_count = total travellers who live in this city (sum across
        # attendees that share it); the score itself is identical for each.
        local_count  = a['count'] + (prev[2] if prev else 0)
        home_scores[home_code] = (display_code, score, local_count)

    ranked_home = []
    for home_code, (display_code, scores, local_count) in sorted(
            home_scores.items(), key=lambda x: (x[1][1][2], x[1][1][3])):
        total_hops, total_dist, total_price, total_carbon = scores

        if display_code.startswith('__rail__'):
            rail_code    = display_code[8:]
            station_info = RAIL_STATIONS.get(rail_code, {})
            city_info    = CITIES.get(rail_code, {})
            iata_field   = None
            rail_field   = rail_code
            city_name    = city_info.get('name', rail_code)
            country      = city_info.get('country', '')
            disp_name    = station_info.get('name', '')
            continent    = 'Europe'
        elif display_code in CITIES:
            cinfo      = CITIES[display_code]
            _airports  = cinfo.get('airports', [])
            iata_field = display_code
            rail_field = None
            city_name  = cinfo['name']
            country    = cinfo['country']
            disp_name  = cinfo['name']
            continent  = (AIRPORTS.get(_airports[0], {}).get('continent', 'Europe')
                          if _airports else 'Europe')
        else:
            info       = AIRPORTS.get(display_code, {})
            iata_field = display_code
            rail_field = None
            city_name  = info.get('city', '')
            country    = info.get('country', '')
            disp_name  = info.get('name', '')
            continent  = info.get('continent', 'Unknown')

        ranked_home.append({
                'iata':        iata_field,
                'rail':        rail_field,
                'city':        city_name,
                'country':     country,
                'name':        disp_name,
                'continent':   continent,
                'home_city':   city_name,
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

    dest_iata may be a standard 3-letter IATA code or a 5-char rail station
    code (e.g. 'GBYOK') when the destination is a rail-only city.

    Each result includes a 'mode' field ('air', 'rail', 'hybrid', or
    'gateway') and each leg carries a 'mode' field so the frontend can show
    ✈ or 🚂 accordingly.
    Rail is preferred for direct connections (1 leg) that are within 30%
    of the air fare, or for multi-leg rail that is outright cheaper.
    """
    # ── Resolve destination using city_code as the primary key ──────────────
    # dest_iata may arrive as:
    #   • a city_code   (e.g. 'FRPAR') — direct CITIES hit
    #   • an airport IATA (e.g. 'CDG') — look up via IATA_TO_CITY
    #   • a non-CITIES airport (e.g. 'JFK') — no CITIES entry; use IATA directly
    _dest_cc = dest_iata if dest_iata in CITIES else IATA_TO_CITY.get(dest_iata)
    if _dest_cc:
        _dest_cinfo   = CITIES[_dest_cc]
        dest_airports = [i for i in _dest_cinfo['airports'] if i in GRAPH]
        dest_rail     = _dest_cinfo.get('rail')
    else:
        # Non-CITIES destination (e.g. overseas airport)
        dest_airports = [dest_iata] if dest_iata in GRAPH else []
        dest_rail     = None

    # Pre-compute rail reachability FROM dest station — used by gateway-hybrid
    # (air-only attendees who need to fly to a nearby hub then train to dest).
    dest_rail_reach = dijkstra_rail_all(dest_rail) if dest_rail else {}

    results = []
    for a in attendees:
        # Home city: any of this destination's airports is one of the attendee's
        # origin airports, OR (for rail-only attendees) the rail stations match.
        is_home = (bool(set(a.get('iatas', [])) & set(dest_airports)) or
                   (dest_rail and dest_rail == a.get('rail') and not a.get('iatas')))
        if is_home:
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
        # dest_airports comes from CITIES, so for Paris (FRPAR) it already
        # includes CDG and ORY; for rail-only cities it is empty.
        best_air_path, best_air_hops, best_air_dist, best_air_origin = None, math.inf, math.inf, None
        _best_air_iata = dest_airports[0] if dest_airports else dest_iata
        for origin_iata in a.get('iatas', []):
            for _air_tgt in dest_airports:
                path, hops, dist = find_best_route(origin_iata, _air_tgt)
                if path is None:
                    continue
                if hops < best_air_hops or (hops == best_air_hops and dist < best_air_dist):
                    best_air_path, best_air_hops, best_air_dist, best_air_origin = path, hops, dist, origin_iata
                    _best_air_iata = _air_tgt

        # ── Best rail route (if available) ──────────────────────────────
        origin_rail = a.get('rail')
        best_rail_path, best_rail_hops, best_rail_dist = find_best_rail_route(origin_rail, dest_rail)

        # ── Hybrid route for rail-only attendees ─────────────────────────
        # When an attendee has no airports, check whether taking the train
        # to a hub and then flying is cheaper than going all the way by rail.
        # This also covers destinations that have no rail station at all.
        best_hybrid: dict | None = None
        if not a.get('iatas') and origin_rail:
            rail_reachable = dijkstra_rail_all(origin_rail)
            _best_hyb_price = math.inf
            for hub_station, (rh, rd) in rail_reachable.items():
                if hub_station == origin_rail:
                    continue
                hub_airports = STATION_TO_IATAS.get(hub_station, [])
                if not hub_airports:
                    continue
                n_rail_xfr = max(0, rh - 1)
                h_rail_price, h_rail_carbon = estimate_rail_fare(rd, n_rail_xfr, origin_rail, hub_station)
                for hub_airport in hub_airports:
                    for _air_dst in dest_airports:
                        air_path, air_hops, air_dist = find_best_route(hub_airport, _air_dst)
                        if air_path is None:
                            continue
                        h_air_price, h_air_carbon = estimate_fare(air_dist, air_hops,
                                                                  hub_airport, _air_dst)
                        total_price = h_rail_price + h_air_price
                        if total_price < _best_hyb_price:
                            _best_hyb_price = total_price
                            hub_rail_path, _, _ = find_best_rail_route(origin_rail, hub_station)
                            best_hybrid = {
                                'rail_path':    hub_rail_path or [],
                                'rail_hops':    rh,
                                'rail_dist':    rd,
                                'air_path':     air_path,
                                'air_hops':     air_hops,
                                'air_dist':     air_dist,
                                'total_price':  total_price,
                                'total_carbon': h_rail_carbon + h_air_carbon,
                                'total_hops':   rh + air_hops,
                                'total_dist':   rd + air_dist,
                            }

        # ── Gateway hybrid: fly to a hub near dest, then take the train ────
        # Fires whenever the destination has a rail station and the attendee
        # has airports to fly from.  We always compute gateway so we can compare
        # it against air and rail and choose the cheapest — this prevents
        # unrealistic all-rail routes like Barcelona → Southampton.
        best_gateway: dict | None = None
        if best_hybrid is None and dest_rail and a.get('iatas') and dest_rail_reach:
            # Precompute air reachability once per origin airport (efficiency:
            # avoids O(origins × gw_airports) find_best_route calls).
            _origin_air_maps: dict = {}
            for _orig in a.get('iatas', []):
                if _orig in GRAPH:
                    _origin_air_maps[_orig] = dijkstra_all(_orig)

            _best_gw_price    = math.inf
            _best_gw_origin   = None
            _best_gw_airport  = None
            _best_gw_station  = None
            _best_gw_rh       = 0
            _best_gw_rd       = 0.0
            _best_gw_air_hops = 0
            _best_gw_air_dist = 0.0
            _best_gw_air_p    = 0.0
            _best_gw_air_c    = 0.0
            _best_gw_rail_p   = 0.0
            _best_gw_rail_c   = 0.0

            for gw_station, (rh, rd) in dest_rail_reach.items():
                if gw_station == dest_rail:
                    continue
                n_rail_xfr = max(0, rh - 1)
                gw_rail_p, gw_rail_c = estimate_rail_fare(rd, n_rail_xfr, gw_station, dest_rail)
                for gw_airport in STATION_TO_IATAS.get(gw_station, []):
                    if gw_airport not in GRAPH:
                        continue
                    for origin_iata, air_map in _origin_air_maps.items():
                        air_result = air_map.get(gw_airport)
                        if air_result is None:
                            continue
                        air_hops, air_dist = air_result
                        a_p, a_c = estimate_fare(air_dist, air_hops,
                                                 origin_iata, gw_airport)
                        total_p = a_p + gw_rail_p
                        if total_p < _best_gw_price:
                            _best_gw_price    = total_p
                            _best_gw_origin   = origin_iata
                            _best_gw_airport  = gw_airport
                            _best_gw_station  = gw_station
                            _best_gw_rh       = rh
                            _best_gw_rd       = rd
                            _best_gw_air_hops = air_hops
                            _best_gw_air_dist = air_dist
                            _best_gw_air_p    = a_p
                            _best_gw_air_c    = a_c
                            _best_gw_rail_p   = gw_rail_p
                            _best_gw_rail_c   = gw_rail_c

            if _best_gw_price < math.inf:
                air_path, _, _ = find_best_route(_best_gw_origin, _best_gw_airport)
                gw_to_dest_path, _, _ = find_best_rail_route(_best_gw_station, dest_rail)
                best_gateway = {
                    'air_path':     air_path or [],
                    'air_hops':     _best_gw_air_hops,
                    'air_dist':     _best_gw_air_dist,
                    'air_price':    _best_gw_air_p,
                    'air_carbon':   _best_gw_air_c,
                    'rail_path':    gw_to_dest_path or [],
                    'rail_hops':    _best_gw_rh,
                    'rail_dist':    _best_gw_rd,
                    'total_price':  _best_gw_price,
                    'total_carbon': _best_gw_air_c + _best_gw_rail_c,
                    'total_hops':   _best_gw_air_hops + _best_gw_rh,
                    'total_dist':   _best_gw_air_dist + _best_gw_rd,
                }

        # ── No route at all ─────────────────────────────────────────────
        if (best_air_path is None and best_rail_path is None
                and best_hybrid is None and best_gateway is None):
            results.append({'city': a['city'], 'count': a['count'],
                            'home': False, 'error': 'No route found', 'legs': []})
            continue

        air_price = air_carbon = math.inf
        if best_air_path is not None:
            air_price, air_carbon = estimate_fare(best_air_dist, best_air_hops,
                                                  best_air_origin, _best_air_iata)

        # ── Decide mode ─────────────────────────────────────────────────
        use_rail    = False
        use_hybrid  = False
        use_gateway = False
        rail_price = rail_carbon = math.inf
        if best_rail_path is not None:
            n_xfr = max(0, best_rail_hops - 1)
            rail_price, rail_carbon = estimate_rail_fare(best_rail_dist, n_xfr, origin_rail, dest_rail)

        gateway_price = best_gateway['total_price'] if best_gateway else math.inf
        hybrid_price  = best_hybrid['total_price']  if best_hybrid  else math.inf

        # Mode selection — fewest hops wins, across single AND mixed modes.
        #
        # First pick the best *single* mode (all-air vs all-rail):
        #   • fewer hops wins, so a 2-hop flight never beats a 1-hop train;
        #   • when rail has MORE hops than the flight, rail only wins if the
        #     whole journey is short (≤ 300 km);
        #   • on equal hops, prefer rail when its fare is within 30% of the
        #     flight.  Otherwise fly.
        # Then pick the best *mixed* mode (hybrid = train→hub→fly, gateway =
        # fly→hub→train), cheaper of the two.
        # Finally choose between them:
        #   • a short pure-rail trip (≤ 300 km) always wins — a quick train
        #     beats any detour;
        #   • otherwise the option with the fewest hops wins, even if that means
        #     a mixed itinerary (e.g. Vienna→Sheffield: fly+train is 2 hops vs
        #     4 hops all-rail);
        #   • on a hop tie, keep the clean single-mode trip.
        _SHORT_RAIL_KM = 300

        single_mode = None          # 'air' | 'rail'
        single_hops = single_dist = math.inf
        if best_air_path is not None or best_rail_path is not None:
            if best_air_path is not None and best_rail_path is not None:
                if best_rail_hops < best_air_hops:
                    pick_rail = True
                elif best_rail_hops > best_air_hops:
                    pick_rail = best_rail_dist <= _SHORT_RAIL_KM
                else:
                    pick_rail = rail_price <= air_price * 1.30
                single_mode = 'rail' if pick_rail else 'air'
            elif best_rail_path is not None:
                single_mode = 'rail'
            else:
                single_mode = 'air'
            if single_mode == 'rail':
                single_hops, single_dist = best_rail_hops, best_rail_dist
            else:
                single_hops, single_dist = best_air_hops, best_air_dist

        mixed_mode = None           # 'hybrid' | 'gateway'
        mixed_hops = math.inf
        if best_hybrid is not None or best_gateway is not None:
            if best_hybrid is not None and (best_gateway is None
                                            or hybrid_price <= gateway_price):
                mixed_mode, mixed_hops = 'hybrid', best_hybrid['total_hops']
            else:
                mixed_mode, mixed_hops = 'gateway', best_gateway['total_hops']

        if single_mode is not None and mixed_mode is not None:
            if single_mode == 'rail' and single_dist <= _SHORT_RAIL_KM:
                chosen = single_mode
            elif mixed_hops < single_hops:
                chosen = mixed_mode
            else:
                chosen = single_mode
        elif single_mode is not None:
            chosen = single_mode
        else:
            chosen = mixed_mode

        use_rail    = chosen == 'rail'
        use_hybrid  = chosen == 'hybrid'
        use_gateway = chosen == 'gateway'

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
        elif use_hybrid:
            hyb = best_hybrid
            legs = []
            # Rail legs: origin station → hub airport station
            for rail_src, rail_dst, dist_km, operator in hyb['rail_path']:
                si = RAIL_STATIONS.get(rail_src, {})
                di = RAIL_STATIONS.get(rail_dst, {})
                legs.append({
                    'src':          rail_src,
                    'dst':          rail_dst,
                    'src_name':     si.get('name', rail_src),
                    'dst_name':     di.get('name', rail_dst),
                    'src_city':     si.get('city', ''),
                    'dst_city':     di.get('city', ''),
                    'src_country':  si.get('country', ''),
                    'dst_country':  di.get('country', ''),
                    'dist_km':      round(dist_km),
                    'airline':      operator,
                    'airline_name': operator,
                    'mode':         'rail',
                })
            # Air legs: hub airport → destination
            for src, dst, dist_km, airline in hyb['air_path']:
                si = AIRPORTS.get(src, {})
                di = AIRPORTS.get(dst, {})
                legs.append({
                    'src':          src,
                    'dst':          dst,
                    'src_name':     si.get('name', src),
                    'dst_name':     di.get('name', dst),
                    'src_city':     si.get('city', ''),
                    'dst_city':     di.get('city', ''),
                    'src_country':  si.get('country', ''),
                    'dst_country':  di.get('country', ''),
                    'dist_km':      round(dist_km),
                    'airline':      airline,
                    'airline_name': AIRLINES.get(airline, airline),
                    'mode':         'air',
                })
            results.append({
                'city':             a['city'],
                'count':            a['count'],
                'home':             False,
                'mode':             'hybrid',
                'hops':             hyb['total_hops'],
                'dist_km':          round(hyb['total_dist']),
                'est_price_person': hyb['total_price'] * 2,
                'est_price_group':  hyb['total_price'] * 2 * a['count'],
                'est_carbon_person':round(hyb['total_carbon'] * 2, 1),
                'est_carbon_group': round(hyb['total_carbon'] * 2 * a['count'], 1),
                'legs':             legs,
            })
        elif use_gateway:
            gw = best_gateway
            legs = []
            # Air legs: origin → gateway airport
            for src, dst, dist_km, airline in gw['air_path']:
                si = AIRPORTS.get(src, {})
                di = AIRPORTS.get(dst, {})
                legs.append({
                    'src':          src,
                    'dst':          dst,
                    'src_name':     si.get('name', src),
                    'dst_name':     di.get('name', dst),
                    'src_city':     si.get('city', ''),
                    'dst_city':     di.get('city', ''),
                    'src_country':  si.get('country', ''),
                    'dst_country':  di.get('country', ''),
                    'dist_km':      round(dist_km),
                    'airline':      airline,
                    'airline_name': AIRLINES.get(airline, airline),
                    'mode':         'air',
                })
            # Rail legs: gateway station → destination
            for rail_src, rail_dst, dist_km, operator in gw['rail_path']:
                si = RAIL_STATIONS.get(rail_src, {})
                di = RAIL_STATIONS.get(rail_dst, {})
                legs.append({
                    'src':          rail_src,
                    'dst':          rail_dst,
                    'src_name':     si.get('name', rail_src),
                    'dst_name':     di.get('name', rail_dst),
                    'src_city':     si.get('city', ''),
                    'dst_city':     di.get('city', ''),
                    'src_country':  si.get('country', ''),
                    'dst_country':  di.get('country', ''),
                    'dist_km':      round(dist_km),
                    'airline':      operator,
                    'airline_name': operator,
                    'mode':         'rail',
                })
            results.append({
                'city':             a['city'],
                'count':            a['count'],
                'home':             False,
                'mode':             'hybrid',
                'hops':             gw['total_hops'],
                'dist_km':          round(gw['total_dist']),
                'est_price_person': gw['total_price'] * 2,
                'est_price_group':  gw['total_price'] * 2 * a['count'],
                'est_carbon_person':round(gw['total_carbon'] * 2, 1),
                'est_carbon_group': round(gw['total_carbon'] * 2 * a['count'], 1),
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

# Per-country rail fare multipliers, applied to the distance-based part of the
# fare (not the fixed booking component, which is roughly universal).
# Calibrated to relative advance-purchase per-km cost across networks:
#   • expensive: UK, Switzerland, Scandinavia (sparse/high-cost networks)
#   • mid: France, Germany, Austria, Benelux (the ~1.0 baseline)
#   • cheap: Southern & Eastern Europe (Italo/Renfe competition, low-cost ops)
# Keyed by the 2-letter ISO prefix of the station code (e.g. 'GBLON' → 'GB').
_RAIL_COUNTRY_PRICE = {
    'GB': 1.55, 'CH': 1.40, 'MC': 1.40, 'NO': 1.35, 'SE': 1.20, 'DK': 1.20,
    'BE': 1.10, 'NL': 1.10, 'LU': 1.10, 'DE': 1.05, 'FR': 1.00, 'AT': 1.00,
    'IT': 0.75, 'ES': 0.80, 'PT': 0.80,
    'PL': 0.60, 'CZ': 0.60, 'SK': 0.60, 'HU': 0.60, 'RO': 0.55,
    'SI': 0.70, 'HR': 0.65, 'RS': 0.60, 'BG': 0.55, 'GR': 0.70, 'TR': 0.55,
    'EE': 0.60, 'LV': 0.60, 'LT': 0.60,
}
_RAIL_COUNTRY_PRICE_DEFAULT = 1.0

# ── Hotel costs ──────────────────────────────────────────────────────────────
# Typical midrange business-hotel nightly rate in USD per city.
# Sourced from aggregated travel-cost data (BudgetYourTrip, Numbeo, Trivago
# averages as of 2025-26).  "Midrange" = clean 3-4★ business hotel, not a
# budget hostel or luxury boutique.
_HOTEL_COSTS: dict[str, int] = {
    # ── United Kingdom ──────────────────────────────────────────────────────
    'GBLON': 220, 'GBEDB': 155, 'GBGLA': 110, 'GBMAN': 120,
    'GBBHM': 105, 'GBBRS': 125, 'GBLED': 105, 'GBNEW':  95,
    'GBLIV': 105, 'GBCDF': 100, 'GBSOU': 100, 'GBSHF':  90,
    'GBNOT':  95, 'GBABZ': 105,
    # ── France ──────────────────────────────────────────────────────────────
    'FRPAR': 200, 'FRLYS': 120, 'FRMRS': 120, 'FRNIC': 165,
    'FRBOD': 110, 'FRTLS': 105, 'FRSXB': 115, 'FRNTE':  95,
    'FRLIL':  95, 'FRMPL': 100, 'FRRNS':  90,
    # ── Belgium / Netherlands ────────────────────────────────────────────────
    'BEBRU': 140, 'NLAMS': 175, 'NLRTM': 120, 'BEANR': 120,
    # ── Luxembourg ──────────────────────────────────────────────────────────
    'LULUX': 160,
    # ── Germany ─────────────────────────────────────────────────────────────
    'DEFRA': 145, 'DEBER': 120, 'DEMUC': 155, 'DEHAM': 135,
    'DECGN': 120, 'DESTT': 120, 'DEDUS': 130, 'DENUR': 105,
    'DEHAN': 105, 'DELEI': 100, 'DEDRS': 100,
    # ── Switzerland ─────────────────────────────────────────────────────────
    'CHZRH': 250, 'CHGVA': 240, 'CHBSL': 195, 'CHBRN': 180, 'CHLAS': 200,
    # ── Austria ─────────────────────────────────────────────────────────────
    'ATVIE': 145, 'ATSBG': 145, 'ATGRZ': 105, 'ATINN': 135,
    # ── Italy ───────────────────────────────────────────────────────────────
    'ITMIL': 155, 'ITROM': 165, 'ITTRN': 105, 'ITFLO': 175,
    'ITVCE': 210, 'ITNAP': 105, 'ITBLN': 120, 'ITGOA': 100, 'ITVRS': 105,
    # ── Monaco ──────────────────────────────────────────────────────────────
    'MCMON': 340,
    # ── Spain / Portugal ────────────────────────────────────────────────────
    'ESMAD': 120, 'ESBCN': 155, 'ESSVQ': 115, 'ESVLC': 100,
    'ESMLG': 105, 'PTLIS': 130, 'PTOPO': 120,
    # ── Czech Republic / Slovakia ────────────────────────────────────────────
    'CZPRG': 100, 'CZBRQ':  75, 'SKBTS':  90,
    # ── Hungary / Romania ────────────────────────────────────────────────────
    'HUBUD':  95, 'ROBUH':  75,
    # ── Poland ──────────────────────────────────────────────────────────────
    'PLWAW':  90, 'PLKRK':  80, 'PLWRO':  80, 'PLGDN':  80,
    # ── Western Balkans / Southeast Europe ──────────────────────────────────
    'SILJB': 105, 'HRZAG':  95, 'RSBEG':  70, 'BGSFP':  65,
    'GRTHE':  80, 'GRATH': 110, 'TRIST':  85,
    # ── Scandinavia ─────────────────────────────────────────────────────────
    'SESTO': 180, 'SEGOT': 155, 'SEMAL': 145, 'DKCPH': 190, 'NOOSL': 200,
    # ── Baltic ──────────────────────────────────────────────────────────────
    'EETAL':  90, 'LVRIX':  85, 'LTVNO':  80,
}

# Monthly seasonal multipliers (index 0 = January, 11 = December).
# European cities are busiest Jul–Aug (+28 %), cheapest Jan and Nov (−15 %).
_HOTEL_SEASONAL = (
    0.85,  # Jan — low season
    0.88,  # Feb
    0.95,  # Mar
    1.05,  # Apr — Easter shoulder
    1.10,  # May
    1.18,  # Jun
    1.28,  # Jul — peak
    1.28,  # Aug — peak
    1.12,  # Sep
    1.00,  # Oct
    0.85,  # Nov — low
    0.90,  # Dec
)


def estimate_hotel_cost(city_code: str, nights: int, outbound_date_str: str) -> int | None:
    """
    Estimated midrange hotel cost per person for the stay, in USD.

    Applies a monthly seasonal multiplier so summer bookings reflect higher
    rack rates and off-peak bookings reflect the discount.  Returns None if
    the city code is not found in _HOTEL_COSTS.
    """
    rate = _HOTEL_COSTS.get(city_code)
    if rate is None:
        return None
    try:
        month = int(outbound_date_str.split('-')[1])  # 1–12
    except (IndexError, ValueError, AttributeError):
        month = 6  # default to June (peak) as a safe overestimate
    month = max(1, min(12, month))
    seasonal = _HOTEL_SEASONAL[month - 1]
    return round(rate * seasonal * nights)


def _rail_price_coeff(*station_codes):
    """
    Average per-country fare multiplier for the given rail endpoints.

    Blends the origin and destination country coefficients so a cross-border
    journey lands between the two networks' price levels (e.g. Paris→Zurich
    averages France 1.00 and Switzerland 1.40 → 1.20). Unknown / missing
    stations contribute the neutral 1.0 baseline.
    """
    coeffs = []
    for code in station_codes:
        if not code:
            continue
        coeffs.append(_RAIL_COUNTRY_PRICE.get(code[:2], _RAIL_COUNTRY_PRICE_DEFAULT))
    if not coeffs:
        return _RAIL_COUNTRY_PRICE_DEFAULT
    return sum(coeffs) / len(coeffs)


def estimate_rail_fare(dist_km, num_transfers=0, origin_station=None, dest_station=None):
    """
    Estimate a one-way European rail fare in USD.

    Calibrated against typical advance-purchase HSR fares (at the neutral
    coefficient of 1.0 — France/Germany level):
      ≤ 300 km: ~$30–50  (short domestic / cross-border)
      ≤ 600 km: ~$55–90  (Eurostar / Thalys / TGV range)
      ≤ 1000 km: ~$80–130 (longer TGV/ICE journeys)
      > 1000 km: ~$110–160 (Paris–Barcelona / Paris–Milan tier)

    When origin_station / dest_station are supplied, the distance-based part of
    the fare is scaled by the blended per-country multiplier (see
    _RAIL_COUNTRY_PRICE), so expensive networks (UK, Switzerland, Scandinavia)
    price higher and cheap ones (Italy, Spain, Eastern Europe) lower. The fixed
    booking component and per-interchange fee are country-neutral.

    num_transfers: number of rail-to-rail interchanges (0 for a direct service).
    Returns (price_usd, carbon_kg_oneway).
    """
    d = dist_km
    if d <= 300:
        base_fixed, per_km = 15, 0.12
    elif d <= 600:
        base_fixed, per_km = 25, 0.09
    elif d <= 1000:
        base_fixed, per_km = 40, 0.075
    else:
        base_fixed, per_km = 55, 0.065

    coeff = _rail_price_coeff(origin_station, dest_station)
    base  = base_fixed + d * per_km * coeff

    transfer_penalty = num_transfers * 15   # $15 per interchange — much cheaper than flight connections
    carbon = round(d * RAIL_CARBON_FACTOR, 1)
    return round(base + transfer_penalty), carbon


def rail_price(origin_station, dest_station, dist_km, transfers,
               outbound_date, return_date):
    """
    Pluggable rail-fare seam — the rail counterpart to serpapi_flight_price().

    The default implementation derives a *round-trip* fare from the
    distance-banded estimator (estimate_rail_fare). To wire in a live rail
    provider (e.g. a Trainline / SNCF / Deutsche Bahn / Rail Europe API),
    replace the body so it queries that provider for the given station pair
    and dates, and returns the same shape — falling back to the estimate on
    any error so the endpoint never hard-fails:

        {'price': <round_trip_usd>, 'carbon_kg': <round_trip_kg>,
         'source': 'live'}

    origin_station / dest_station are 5-char rail station codes; transfers is
    the number of rail-to-rail interchanges. Price and carbon are round-trip.
    """
    oneway_price, oneway_carbon = estimate_rail_fare(dist_km, transfers,
                                                     origin_station, dest_station)
    return {
        'price':     oneway_price * 2,
        'carbon_kg': round(oneway_carbon * 2, 1),
        'source':    'estimate',
    }


# Each extra rail hop is penalised by this many km when choosing between routes.
# Rationale: without a penalty, pure distance-first routing displaces direct
# services (e.g. Berlin→Munich ICE, Frankfurt→Vienna Railjet) with marginally
# shorter multi-hop paths via intermediate cities.  A 75 km penalty keeps those
# direct services while still rerouting dramatic Paris-hub detours (Nice→Toulouse
# via Paris saves ~1000 km even after the penalty).  Valid range: 64–85 km.
_RAIL_HOP_PENALTY_KM = 75


def dijkstra_rail_all(origin_station):
    """
    Dijkstra over RAIL_GRAPH from origin_station.
    Returns best[(station_code)] = (hops, total_dist_km) for all reachable stations.

    Uses effective_cost = actual_dist + hops * _RAIL_HOP_PENALTY_KM as the
    routing objective so that (a) dramatic geographic shortcuts are always taken
    and (b) marginal shortcuts that displace a real direct service are not.
    """
    if origin_station not in RAIL_GRAPH:
        return {}
    INF = math.inf
    # best[station] = (eff_cost, hops, actual_dist)
    best = {origin_station: (0.0, 0, 0.0)}
    heap = [(0.0, 0, 0.0, origin_station)]      # (eff_cost, hops, actual_dist, station)
    while heap:
        eff, hops, dist, current = heapq.heappop(heap)
        b_eff, b_hops, _ = best.get(current, (INF, INF, INF))
        if eff > b_eff or (eff == b_eff and hops > b_hops):
            continue
        for (neighbour, edge_dist, operator) in RAIL_GRAPH.get(current, []):
            n_hops = hops + 1
            n_dist = dist + edge_dist
            n_eff  = n_dist + n_hops * _RAIL_HOP_PENALTY_KM
            b = best.get(neighbour, (INF, INF, INF))
            if n_eff < b[0] or (n_eff == b[0] and n_hops < b[1]):
                best[neighbour] = (n_eff, n_hops, n_dist)
                heapq.heappush(heap, (n_eff, n_hops, n_dist, neighbour))
    # Return in callers' expected format: (hops, actual_dist)
    return {s: (h, d) for s, (_, h, d) in best.items()}


def find_best_rail_route(origin_station, dest_station):
    """
    Find the best rail route between two rail stations.

    Runs Dijkstra over RAIL_GRAPH.  Returns:
      (path, hops, total_dist_km)  — path is a list of (src, dst, dist_km, operator)
      (None, None, None)           — if no rail connection exists
    """
    if not origin_station or not dest_station or origin_station == dest_station:
        return None, None, None

    INF = math.inf
    # heap: (eff_cost, hops, actual_dist, station, path)
    heap = [(0.0, 0, 0.0, origin_station, [])]
    visited = {}
    while heap:
        eff, hops, total_dist, current, path = heapq.heappop(heap)
        if current in visited:
            p_eff, p_hops = visited[current]
            if eff > p_eff or (eff == p_eff and hops >= p_hops):
                continue
        visited[current] = (eff, hops)
        if current == dest_station:
            return path, hops, total_dist
        for (neighbour, dist, operator) in RAIL_GRAPH.get(current, []):
            n_hops = hops + 1
            n_dist = total_dist + dist
            n_eff  = n_dist + n_hops * _RAIL_HOP_PENALTY_KM
            if neighbour in visited:
                p_eff, p_hops = visited[neighbour]
                if n_eff > p_eff or (n_eff == p_eff and n_hops >= p_hops):
                    continue
            heapq.heappush(heap, (
                n_eff, n_hops, n_dist, neighbour,
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
    for a in attendees:
        if not isinstance(a.get('city'), str) or not a['city'].strip():
            return jsonify({'error': 'Each attendee must have a city name.'}), 400
        if not isinstance(a.get('count'), int) or a['count'] < 1:
            return jsonify({'error': 'Each attendee count must be a whole number ≥ 1.'}), 400
    try:
        nights = int(data.get('nights', 0))
    except (TypeError, ValueError):
        nights = 0
    nights = max(0, min(30, nights))
    log.info("find_destinations: %d attendees, continent_filter=%s, nights=%d",
             len(attendees), continent_filter, nights)
    ranked, ranked_home = find_meeting_destinations(
        attendees, continent_filter=continent_filter, nights=nights)
    return jsonify({'overall': ranked, 'home': ranked_home,
                    'continent_filter': continent_filter, 'nights': nights})

@app.route('/api/get_routes', methods=['POST'])
@limiter.limit("60/minute")
def get_routes():
    data      = request.json
    attendees = data.get('attendees', [])
    dest_iata = data.get('dest_iata', '')
    if not dest_iata or not attendees:
        return jsonify({'error': 'Missing data.'}), 400

    dest_info = AIRPORTS.get(dest_iata)
    dest_is_rail = False
    if dest_info is None and dest_iata in RAIL_STATIONS:
        # Rail-station destination (e.g. 'GBYOK' for York)
        station   = RAIL_STATIONS[dest_iata]
        city_info = CITIES.get(dest_iata, {})
        dest_info = {
            'city':      city_info.get('name', station.get('city', dest_iata)),
            'country':   city_info.get('country', station.get('country', '')),
            'name':      station.get('name', dest_iata),
            'continent': 'Europe',
        }
        dest_is_rail = True
    else:
        dest_info = dest_info or {}

    routes = get_routes_for_destination(attendees, dest_iata)
    return jsonify({'dest': dest_info, 'dest_iata': dest_iata,
                    'dest_is_rail': dest_is_rail, 'routes': routes})

@app.route('/api/get_live_prices', methods=['POST'])
@limiter.limit("20/minute")
def get_live_prices():
    """
    Price each attendee's journey per travel mode.

    Reuses get_routes_for_destination() so live pricing follows exactly the
    same mode decision (air / rail / hybrid / gateway) as the route drill-down.
    Each route's legs are grouped into maximal same-mode segments; air segments
    are priced via SerpApi Google Flights (with a distance-estimate fallback)
    and rail segments via the pluggable rail_price() seam. Hybrid/gateway
    journeys are the sum of their air and rail segments. Carbon is taken from
    the route estimate.
    """
    data        = request.json
    attendees   = data.get('attendees', [])
    dest_iata   = data.get('dest_iata', '')
    try:
        weeks_ahead = int(data.get('weeks_ahead', 8))
    except (TypeError, ValueError):
        return jsonify({'error': 'Weeks ahead must be a whole number.'}), 400
    if not 1 <= weeks_ahead <= 52:
        return jsonify({'error': 'Weeks ahead must be between 1 and 52.'}), 400
    try:
        nights = int(data.get('nights', 2))
    except (TypeError, ValueError):
        nights = 2
    nights = max(0, min(30, nights))

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

    routes       = get_routes_for_destination(attendees, dest_iata)
    results      = []
    total_price  = 0
    total_carbon = 0.0

    for r in routes:
        if r.get('home'):
            results.append({'city': r['city'], 'count': r['count'],
                            'home': True, 'price_per_person': 0,
                            'total_price': 0, 'carbon_kg_person': 0,
                            'carbon_kg_group': 0, 'source': 'home'})
            continue

        legs = r.get('legs', [])
        if not legs:
            results.append({'city': r['city'], 'count': r['count'],
                            'error': 'No route found'})
            continue

        # Group consecutive legs of the same mode into maximal segments so an
        # air leg-chain is priced as a single through-journey and a rail
        # leg-chain as a single ticket.
        segments: list[dict] = []
        for leg in legs:
            if segments and segments[-1]['mode'] == leg['mode']:
                seg = segments[-1]
                seg['dst']      = leg['dst']
                seg['dist_km'] += leg['dist_km']
                seg['legs']    += 1
            else:
                segments.append({'mode': leg['mode'], 'src': leg['src'],
                                 'dst': leg['dst'], 'dist_km': leg['dist_km'],
                                 'legs': 1})

        # Price and carbon each segment by its mode and sum (all round-trip).
        # Carbon uses Google Flights data when available for air segments,
        # falling back to the distance estimate; rail always uses the estimate.
        price_per_person  = 0
        carbon_per_person = 0.0
        carbon_live       = True   # flips False if any segment falls back to estimate
        sources: set[str] = set()
        for seg in segments:
            if seg['mode'] == 'air':
                price_data = serpapi_flight_price(seg['src'], seg['dst'],
                                                  outbound_str, return_str)
                if 'error' not in price_data:
                    price_per_person += price_data['price']
                    sources.add('live')
                    # Use Google Flights carbon when provided; fall back to estimate
                    carbon_g = price_data.get('carbon_g')
                    if carbon_g:
                        carbon_per_person += round((carbon_g * 2) / 1000, 1)
                    else:
                        _, oneway_carbon = estimate_fare(seg['dist_km'], seg['legs'],
                                                         seg['src'], seg['dst'])
                        carbon_per_person += round(oneway_carbon * 2, 1)
                        carbon_live = False
                else:
                    oneway_price, oneway_carbon = estimate_fare(seg['dist_km'], seg['legs'],
                                                                seg['src'], seg['dst'])
                    price_per_person  += oneway_price * 2
                    carbon_per_person += round(oneway_carbon * 2, 1)
                    carbon_live = False
                    sources.add('estimate')
                    log.warning("SerpApi failed for %s->%s, using estimate: %s",
                                seg['src'], seg['dst'], price_data['error'])
            else:  # rail — carbon always estimated
                rp = rail_price(seg['src'], seg['dst'], seg['dist_km'],
                                max(0, seg['legs'] - 1), outbound_str, return_str)
                price_per_person  += rp['price']
                carbon_per_person += rp['carbon_kg']
                carbon_live = False
                sources.add(rp['source'])

        carbon_per_person = round(carbon_per_person, 1) if carbon_per_person else None

        if sources == {'live'}:
            source = 'live'
        elif 'live' in sources:
            source = 'mixed'          # live air + estimated rail (or fallback)
        else:
            source = 'estimate'
        group_total  = price_per_person * r['count']
        group_carbon = round(carbon_per_person * r['count'], 1) if carbon_per_person else None
        total_price += group_total
        if group_carbon: total_carbon += group_carbon

        # Extract city names and airport codes for the route label.
        air_legs     = [l for l in legs if l['mode'] == 'air']
        origin_city  = legs[0].get('src_city') or legs[0]['src']
        dest_city    = legs[-1].get('dst_city') or legs[-1]['dst']
        origin_code  = legs[0]['src']                            # IATA or station code
        dest_code    = air_legs[-1]['dst'] if air_legs else None # last air leg dst IATA

        results.append({
            'city':             r['city'],
            'count':            r['count'],
            'home':             False,
            'mode':             r.get('mode'),
            'origin':           origin_code,
            'origin_city':      origin_city,
            'dest_city':        dest_city,
            'dest_airport':     dest_code,
            'dist_km':          r.get('dist_km'),
            'price_per_person': price_per_person,
            'total_price':      group_total,
            'carbon_kg_person': carbon_per_person,
            'carbon_kg_group':  group_carbon,
            'outbound':         outbound_str,
            'return_date':      return_str,
            'source':           source,
        })

    # ── Hotel estimate for the destination ───────────────────────────────────
    dest_city_code = dest_iata if dest_iata in CITIES else IATA_TO_CITY.get(dest_iata)
    hotel_per_person = estimate_hotel_cost(dest_city_code, nights, outbound_str) if dest_city_code else None
    # Count non-home travellers
    travelling_count = sum(
        r['count'] for r in results
        if not r.get('home') and not r.get('error')
    )
    hotel_total = hotel_per_person * travelling_count if hotel_per_person is not None else None
    dest_city_name = CITIES[dest_city_code]['name'] if dest_city_code and dest_city_code in CITIES else None

    return jsonify({
        'results':              results,
        'total_price':          total_price,
        'total_carbon_kg':      round(total_carbon, 1) if total_carbon else None,
        'dest_iata':            dest_iata,
        'outbound_date':        outbound_str,
        'return_date':          return_str,
        'hotel_per_person':     hotel_per_person,
        'hotel_total':          hotel_total,
        'hotel_nights':         nights,
        'hotel_city':           dest_city_name,
        'travelling_count':     travelling_count,
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
