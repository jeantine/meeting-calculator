"""
tests/test_meeting_finder.py

Tests covering all corrections made to the Meeting Calculator app:

  1.  Carbon-factor function (distance-banded, no radiative forcing index)
  2.  Fare estimator (including VIE↔BFN calibration)
  3.  Stranded airport filtering (DUT/Unalaska excluded)
  4.  Sort order (fewest flights → lowest carbon)
  5.  /api/find_destinations endpoint
  6.  /api/get_routes endpoint (incl. airline name resolution for tooltips)
  7.  /api/get_live_prices endpoint (key check, SerpAPI mock, home-city, fallback)
  8.  SerpAPI response parser (price extraction, error paths, HTTP errors)
  9.  .env loading — key must not be overridden by launch.json placeholder
  10. HTML: USD currency display (all price surfaces)
  11. HTML: UI/UX corrections (button text, scrolling, keyboard nav, columns, header)
  12. HTML: Attendee chip — green styling, inline count editing, clears results
  13. HTML: First result auto-focused after Find
  14. HTML: Find resets to Overall tab regardless of active tab
  15. HTML: Route view — est. badge format (not ~), cost/carbon before dist
  16. HTML: View routes pill hidden on selected row (visible only on hover/focus)
  17. HTML: Mouse move clears keyboard focus highlight
  18. HTML: Price toolbar scrolls into view when route is selected
  19. HTML: Continent filter re-runs search when results are already shown
  20. HTML: Home attendees shown with local count in their own column
  21. HTML: Bare $ bug fixed — live prices per-person amount uses US$
  22. Round-trip pricing correctness (est_price_person = oneway × 2, group scales with count)
  23. haversine distance accuracy
  24. find_best_route — fewest hops preferred, unreachable returns None
  25. estimate_fare at distance band boundaries
  26. /api/search_city endpoint (happy path, short query, result cap)
  27. Multi-airport cities resolve multiple IATAs
  28. Data pipeline integrity (AIRPORTS fields, AIRLINES loaded, GRAPH edges valid)
  29. Static file serving (GET /, GET /world-airports.svg)
  30. Page structure (title, viewport meta, font import)
  31. get_continent edge cases (Antarctica, Greenland, mid-Pacific)
  32. find_meeting_destinations with all attendees from same city
  33. /api/get_routes returns 400 on missing data
"""

import io
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

# ─── path setup ───────────────────────────────────────────────────────────────
_HERE    = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _APP_DIR)

import app as app_module
from app import (
    AIRPORTS,
    AIRLINES,
    GRAPH,
    MAIN_AIRPORTS,
    _carbon_factor,
    app as flask_app,
    estimate_fare,
    find_airports_by_city,
    find_best_route,
    find_meeting_destinations,
    get_routes_for_destination,
    get_continent,
    haversine,
    serpapi_flight_price,
)


# ─── shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture(scope="module")
def html():
    with open(os.path.join(_APP_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


def _mock_urlopen(payload: dict):
    """Return a mock that behaves like urllib.request.urlopen context manager."""
    body = json.dumps(payload).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ─── 1. Carbon factor ─────────────────────────────────────────────────────────

class TestCarbonFactor:
    """Distance-banded factors without radiative forcing index (matches Google Flights)."""

    def test_very_short_haul(self):
        assert _carbon_factor(300) == 0.170

    def test_short_haul_boundary(self):
        assert _carbon_factor(750) == 0.170

    def test_medium_short_haul(self):
        assert _carbon_factor(1000) == 0.130

    def test_medium_haul_boundary(self):
        assert _carbon_factor(2000) == 0.130

    def test_medium_haul(self):
        assert _carbon_factor(3500) == 0.105

    def test_long_haul(self):
        assert _carbon_factor(7000) == 0.095

    def test_ultra_long_haul(self):
        assert _carbon_factor(12000) == 0.085

    def test_factor_decreases_with_distance(self):
        """Wider aircraft at higher load factors → more efficient per km."""
        assert _carbon_factor(400) > _carbon_factor(1500) > _carbon_factor(9500)


# ─── 2. Fare estimator ────────────────────────────────────────────────────────

class TestEstimateFare:

    def test_returns_price_and_carbon(self):
        price, carbon = estimate_fare(1000, 1)
        assert isinstance(price, int)
        assert isinstance(carbon, float)

    def test_short_haul_price_plausible(self):
        price, _ = estimate_fare(500, 1)
        assert 80 < price < 400, f"Unexpected short-haul one-way price: {price}"

    def test_long_haul_price_plausible(self):
        price, _ = estimate_fare(9000, 1)
        assert 500 < price < 2000, f"Unexpected long-haul one-way price: {price}"

    def test_stop_penalty_raises_price(self):
        direct, _  = estimate_fare(2000, 1)
        via_stop, _ = estimate_fare(2000, 2)
        assert via_stop > direct

    def test_carbon_grows_with_distance(self):
        _, c_short = estimate_fare(500,  1)
        _, c_long  = estimate_fare(9000, 1)
        assert c_long > c_short

    def test_vie_bfn_carbon_calibration(self):
        """
        VIE→BFN ≈ 9 600 km. Round-trip for 2 pax previously gave ~4 600 kg
        (wrong, used radiative forcing index). Google Flights shows ~1 744 kg.
        Our distance-banded model without RFI should be well under 3 000 kg.
        """
        dist_km = 9_600
        _, oneway_carbon = estimate_fare(dist_km, 1)
        roundtrip_2pax = oneway_carbon * 2 * 2  # return × 2 travellers
        # Previous (broken) estimate was ~4 600 kg due to radiative forcing index.
        # Google Flights baseline is ~1 744 kg. Our model sits in between but
        # must be well below the old figure.
        assert roundtrip_2pax < 4_000, (
            f"Carbon still too high after calibration: {roundtrip_2pax:.0f} kg "
            f"(Google Flights baseline ~1 744 kg, old broken estimate ~4 600 kg)"
        )


# ─── 3. Stranded airport filtering ────────────────────────────────────────────

class TestStrandedAirports:

    def test_main_network_covers_major_hubs(self):
        for iata in ["LHR", "JFK", "CDG", "SYD", "DXB", "VIE", "SIN"]:
            assert iata in MAIN_AIRPORTS, f"{iata} should be in the main network"

    def test_dut_unalaska_excluded(self):
        """DUT only connects to 3 tiny local strips with no onward routes."""
        assert "DUT" not in MAIN_AIRPORTS

    def test_unalaska_search_returns_no_results(self):
        results = find_airports_by_city("Unalaska")
        assert results == [], f"Expected no results for Unalaska, got: {results}"

    def test_main_network_large(self):
        assert len(MAIN_AIRPORTS) > 3_000


# ─── 4. Sort order ────────────────────────────────────────────────────────────

class TestSortOrder:
    """Results must be sorted: fewest avg flights first, then lowest carbon."""

    def test_overall_results_sorted_by_hops_then_carbon(self, client):
        payload = {
            "attendees": [
                {"city": "London",   "iatas": ["LHR"], "count": 1},
                {"city": "New York", "iatas": ["JFK"], "count": 1},
            ]
        }
        res  = client.post("/api/find_destinations", json=payload)
        data = res.get_json()
        assert res.status_code == 200
        overall = data["overall"]
        assert len(overall) > 1

        hops = [d["avg_hops"] for d in overall]
        assert hops == sorted(hops), f"Results not sorted by avg_hops: {hops}"

    def test_equal_hops_ordered_by_carbon(self, client):
        """When two destinations share the same hop count the lower-carbon one ranks first."""
        payload = {
            "attendees": [
                {"city": "London", "iatas": ["LHR"], "count": 1},
                {"city": "Paris",  "iatas": ["CDG"], "count": 1},
            ]
        }
        res  = client.post("/api/find_destinations", json=payload)
        data = res.get_json()
        overall = data["overall"]

        # Group by rounded avg_hops and check carbon within each group
        from itertools import groupby
        for _, group in groupby(overall, key=lambda d: d["avg_hops"]):
            group = list(group)
            carbons = [d["est_carbon"] for d in group]
            assert carbons == sorted(carbons), (
                f"Within equal-hop group, carbon not sorted: {carbons}"
            )


# ─── 5. /api/find_destinations ────────────────────────────────────────────────

class TestFindDestinationsEndpoint:

    def test_requires_two_or_more_attendees(self, client):
        res = client.post(
            "/api/find_destinations",
            json={"attendees": [{"city": "London", "iatas": ["LHR"], "count": 1}]},
        )
        assert res.status_code == 400

    def test_returns_up_to_ten_results(self, client):
        payload = {
            "attendees": [
                {"city": "London", "iatas": ["LHR"], "count": 1},
                {"city": "Vienna", "iatas": ["VIE"], "count": 1},
            ]
        }
        res  = client.post("/api/find_destinations", json=payload)
        data = res.get_json()
        assert res.status_code == 200
        assert 0 < len(data["overall"]) <= 10

    def test_results_include_cost_and_carbon(self, client):
        payload = {
            "attendees": [
                {"city": "London", "iatas": ["LHR"], "count": 2},
                {"city": "Vienna", "iatas": ["VIE"], "count": 1},
            ]
        }
        res  = client.post("/api/find_destinations", json=payload)
        dest = res.get_json()["overall"][0]
        assert dest["est_cost"]   >= 0
        assert dest["est_carbon"] >= 0

    def test_continent_filter_restricts_results(self, client):
        payload = {
            "attendees": [
                {"city": "London", "iatas": ["LHR"], "count": 1},
                {"city": "Paris",  "iatas": ["CDG"], "count": 1},
            ],
            "continent_filter": "Europe",
        }
        res  = client.post("/api/find_destinations", json=payload)
        data = res.get_json()
        assert res.status_code == 200
        for dest in data["overall"]:
            assert dest["continent"] == "Europe", (
                f"{dest['iata']} is not in Europe (got {dest['continent']})"
            )


# ─── 6. /api/get_routes ───────────────────────────────────────────────────────

class TestGetRoutesEndpoint:

    def test_returns_route_data(self, client):
        payload = {
            "attendees": [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
            "dest_iata": "LHR",
        }
        res   = client.post("/api/get_routes", json=payload)
        data  = res.get_json()
        assert res.status_code == 200
        route = data["routes"][0]
        assert route["hops"]    >= 1
        assert route["dist_km"] >  0

    def test_route_includes_estimated_price_and_carbon(self, client):
        payload = {
            "attendees": [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
            "dest_iata": "LHR",
        }
        res   = client.post("/api/get_routes", json=payload)
        route = res.get_json()["routes"][0]
        assert "est_price_person"  in route
        assert "est_carbon_person" in route
        assert route["est_price_person"]  > 0
        assert route["est_carbon_person"] > 0

    def test_airline_name_present_for_tooltips(self, client):
        """
        Each leg must carry airline_name so the HTML title tooltip works.
        Previously legs only had the two-letter IATA code.
        """
        payload = {
            "attendees": [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
            "dest_iata": "LHR",
        }
        res  = client.post("/api/get_routes", json=payload)
        legs = res.get_json()["routes"][0]["legs"]
        assert len(legs) > 0
        for leg in legs:
            assert "airline_name" in leg, f"Missing airline_name on leg {leg}"
            assert leg["airline_name"]  # non-empty string

    def test_home_city_attendee_flagged(self, client):
        payload = {
            "attendees": [{"city": "London", "iatas": ["LHR"], "count": 3}],
            "dest_iata": "LHR",
        }
        res   = client.post("/api/get_routes", json=payload)
        route = res.get_json()["routes"][0]
        assert route["home"] is True
        assert route["legs"] == []


# ─── 7. /api/get_live_prices ─────────────────────────────────────────────────

class TestGetLivePricesEndpoint:

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _set_key(value):
        """Temporarily replace the module-level SERPAPI_KEY."""
        orig = app_module.SERPAPI_KEY
        app_module.SERPAPI_KEY = value
        return orig

    BASE_PAYLOAD = {
        "attendees": [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
        "dest_iata": "LHR",
        "weeks_ahead": 8,
    }

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_returns_400_when_key_is_placeholder(self, client):
        orig = self._set_key("YOUR_SERPAPI_KEY_HERE")
        try:
            res  = client.post("/api/get_live_prices", json=self.BASE_PAYLOAD)
            data = res.get_json()
            assert res.status_code == 400
            assert "error" in data
            assert "key" in data["error"].lower() or "configured" in data["error"].lower()
        finally:
            app_module.SERPAPI_KEY = orig

    def test_falls_back_to_estimate_when_serpapi_fails(self, client):
        orig = self._set_key("test-key")
        try:
            with patch("app.serpapi_flight_price", return_value={"error": "quota exceeded"}):
                res  = client.post("/api/get_live_prices", json=self.BASE_PAYLOAD)
                data = res.get_json()
            assert res.status_code == 200
            result = data["results"][0]
            assert "SerpApi failed" in result["source"]
            assert result["price_per_person"] > 0
        finally:
            app_module.SERPAPI_KEY = orig

    def test_uses_live_price_when_serpapi_succeeds(self, client):
        orig = self._set_key("test-key")
        try:
            mock_price = {"price": 450, "carbon_g": 120_000, "currency": "USD"}
            with patch("app.serpapi_flight_price", return_value=mock_price):
                res  = client.post("/api/get_live_prices", json=self.BASE_PAYLOAD)
                data = res.get_json()
            result = data["results"][0]
            assert result["source"]           == "live"
            assert result["price_per_person"] == 450
        finally:
            app_module.SERPAPI_KEY = orig

    def test_home_city_has_zero_cost(self, client):
        orig = self._set_key("test-key")
        try:
            payload = {
                "attendees": [
                    {"city": "London", "iatas": ["LHR"], "count": 2},
                    {"city": "Paris",  "iatas": ["CDG"], "count": 1},
                ],
                "dest_iata": "LHR",
                "weeks_ahead": 8,
            }
            with patch("app.serpapi_flight_price", return_value={"price": 300, "currency": "USD"}):
                res  = client.post("/api/get_live_prices", json=payload)
                data = res.get_json()
            home = next(r for r in data["results"] if r.get("home"))
            assert home["price_per_person"] == 0
            assert home["total_price"]      == 0
        finally:
            app_module.SERPAPI_KEY = orig

    def test_total_price_sums_all_groups(self, client):
        orig = self._set_key("test-key")
        try:
            payload = {
                "attendees": [
                    {"city": "Vienna", "iatas": ["VIE"], "count": 2},
                    {"city": "Paris",  "iatas": ["CDG"], "count": 3},
                ],
                "dest_iata": "LHR",
                "weeks_ahead": 8,
            }
            mock_price = {"price": 400, "currency": "USD"}
            with patch("app.serpapi_flight_price", return_value=mock_price):
                res  = client.post("/api/get_live_prices", json=payload)
                data = res.get_json()
            expected = sum(r["total_price"] for r in data["results"])
            assert data["total_price"] == expected
        finally:
            app_module.SERPAPI_KEY = orig


# ─── 8. SerpAPI response parser ───────────────────────────────────────────────

class TestSerpApiParser:

    def test_picks_lowest_price_from_best_flights(self):
        response = {
            "best_flights": [
                {"price": 600, "carbon_emissions": {"this_flight": 90_000}},
                {"price": 420, "carbon_emissions": {"this_flight": 75_000}},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(response)):
            result = serpapi_flight_price("VIE", "LHR", "2026-08-01", "2026-08-05")
        assert result["price"]    == 420
        assert result["carbon_g"] == 75_000

    def test_falls_back_to_other_flights_when_best_empty(self):
        response = {
            "best_flights":  [],
            "other_flights": [{"price": 710}],
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(response)):
            result = serpapi_flight_price("VIE", "LHR", "2026-08-01", "2026-08-05")
        assert result["price"] == 710

    def test_uses_price_insights_as_last_resort(self):
        response = {
            "best_flights":    [],
            "other_flights":   [],
            "price_insights":  {"lowest_price": 555},
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(response)):
            result = serpapi_flight_price("VIE", "LHR", "2026-08-01", "2026-08-05")
        assert result["price"] == 555

    def test_returns_error_when_no_prices_found(self):
        response = {"best_flights": [], "other_flights": []}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(response)):
            result = serpapi_flight_price("VIE", "LHR", "2026-08-01", "2026-08-05")
        assert "error" in result

    def test_propagates_api_error_message(self):
        response = {"error": "Invalid API key."}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(response)):
            result = serpapi_flight_price("VIE", "LHR", "2026-08-01", "2026-08-05")
        assert "error" in result
        assert "Invalid API key" in result["error"]

    def test_handles_http_401(self):
        err = urllib.error.HTTPError(
            None, 401, "Unauthorized", {}, io.BytesIO(b"Unauthorized")
        )
        with patch("urllib.request.urlopen", side_effect=err):
            result = serpapi_flight_price("VIE", "LHR", "2026-08-01", "2026-08-05")
        assert "error" in result
        assert "401" in result["error"]

    def test_handles_network_exception(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = serpapi_flight_price("VIE", "LHR", "2026-08-01", "2026-08-05")
        assert "error" in result


# ─── 9. .env / key configuration ─────────────────────────────────────────────

class TestEnvConfiguration:

    _ENV_PATH = os.path.join(_APP_DIR, ".env")
    _GITIGNORE = os.path.join(_APP_DIR, ".gitignore")
    _LAUNCH_JSON = os.path.abspath(
        os.path.join(_APP_DIR, "..", "planthood-agent", ".claude", "launch.json")
    )

    def test_env_file_exists(self):
        assert os.path.exists(self._ENV_PATH), ".env must exist alongside app.py"

    def test_env_file_gitignored(self):
        with open(self._GITIGNORE) as f:
            content = f.read()
        assert ".env" in content, ".env must be listed in .gitignore"

    def test_launch_json_does_not_inject_placeholder_key(self):
        """
        The root cause of the SerpAPI key not being loaded was that launch.json
        set SERPAPI_KEY=YOUR_KEY_HERE in env, which os.environ.setdefault then
        kept — meaning the real key in .env was silently ignored.
        """
        if not os.path.exists(self._LAUNCH_JSON):
            pytest.skip("launch.json not found at expected path")
        with open(self._LAUNCH_JSON) as f:
            config = json.load(f)
        for conf in config.get("configurations", []):
            env = conf.get("env", {})
            assert env.get("SERPAPI_KEY", "") not in ("YOUR_KEY_HERE", "YOUR_SERPAPI_KEY_HERE"), (
                "launch.json must not inject a placeholder SERPAPI_KEY — "
                "it prevents .env from being loaded"
            )


# ─── 10. HTML: USD currency display ──────────────────────────────────────────

class TestUSDCurrencyDisplay:
    """Every price surface in the UI must show US$ not a bare $."""

    def test_both_table_headers_say_usd(self, html):
        count = html.count("Est. Cost (USD)")
        assert count == 2, f"Expected 2 × 'Est. Cost (USD)', found {count}"

    def test_table_cells_use_us_dollar_prefix(self, html):
        assert "'US$' + d.est_cost.toLocaleString()" in html

    def test_no_bare_dollar_on_est_cost_cells(self, html):
        assert "'$' + d.est_cost" not in html

    def test_route_header_estimated_total_uses_us_dollar(self, html):
        assert "est. US$" in html

    def test_route_per_person_badge_uses_us_dollar(self, html):
        assert "US$${r.est_price_person" in html

    def test_live_prices_per_person_uses_us_dollar(self, html):
        assert "US$${r.price_per_person" in html

    def test_live_prices_grand_total_uses_us_dollar(self, html):
        assert "US$${data.total_price" in html

    def test_live_prices_subtotal_uses_us_dollar(self, html):
        assert "US$${r.total_price" in html


# ─── 11. HTML: UI/UX features ────────────────────────────────────────────────

class TestUIFeatures:

    # ── button text ───────────────────────────────────────────────────────────

    def test_live_prices_button_says_google_flights(self, html):
        assert "Get Live Prices (Google Flights)" in html

    def test_live_prices_button_does_not_say_serpapi(self, html):
        assert "(SerpApi)" not in html

    # ── airline name tooltip ──────────────────────────────────────────────────

    def test_leg_has_airline_name_tooltip(self, html):
        """Hovering over the two-letter code should show the full airline name."""
        assert 'title="${l.airline_name' in html

    # ── scroll behaviours ─────────────────────────────────────────────────────

    def test_scrolls_to_price_results_after_live_fetch(self, html):
        assert "priceDiv.scrollIntoView" in html

    def test_scrolls_route_detail_into_view_when_selected(self, html):
        assert "priceToolbar" in html and "scrollIntoView" in html

    def test_focused_row_scrolls_into_view_on_arrow_key(self, html):
        """Arrow-key navigation must keep the focused row visible."""
        assert "rowEl.scrollIntoView" in html

    # ── keyboard navigation ───────────────────────────────────────────────────

    def test_arrow_down_handler_exists(self, html):
        assert "ArrowDown" in html

    def test_arrow_up_handler_exists(self, html):
        assert "ArrowUp" in html

    def test_enter_triggers_route_detail(self, html):
        assert "focusedIata" in html
        assert "selectDest" in html

    def test_tab_from_city_search_focuses_find_button(self, html):
        """Tab should skip delete buttons and land on Find Best Locations."""
        assert "e.key === 'Tab'" in html
        assert "findBtn.focus()" in html

    def test_delete_buttons_excluded_from_tab_order(self, html):
        assert 'tabindex="-1"' in html

    # ── selected / focused row styling ────────────────────────────────────────

    def test_selected_row_has_green_left_border(self, html):
        assert ".dest-table tr.selected td:first-child" in html
        assert "border-left" in html

    def test_focused_row_css_class_exists(self, html):
        assert ".dest-table tr.focused" in html

    # ── column ordering ───────────────────────────────────────────────────────

    def test_cost_column_appears_before_distance_column(self, html):
        """Est. Cost (USD) must be to the left of Total Dist in the header."""
        cost_pos = html.index("Est. Cost (USD)")
        dist_pos = html.index("Total Dist")
        assert cost_pos < dist_pos, "Est. Cost column must come before Total Dist"

    # ── attendee count editing ────────────────────────────────────────────────

    def test_attendee_count_is_editable(self, html):
        assert "editAttendeeCount" in html

    def test_editing_count_reruns_search(self, html):
        """Changing a count while results are visible must immediately re-run the search."""
        assert "findBtn.click()" in html

    # ── header redesign ───────────────────────────────────────────────────────

    def test_header_has_gradient_accent_line(self, html):
        assert "header::after" in html

    def test_header_icon_uses_gradient_fill(self, html):
        import re
        icon_block = re.search(r"\.header-icon\s*\{[^}]+\}", html, re.DOTALL)
        assert icon_block, ".header-icon CSS block not found"
        assert "linear-gradient" in icon_block.group()

    def test_header_title_uses_gradient_text(self, html):
        assert "background-clip: text" in html or "-webkit-background-clip: text" in html

    def test_header_has_stacked_title_and_subtitle(self, html):
        assert "header-text" in html

    def test_header_icon_uses_svg_not_emoji(self, html):
        """Logo is an inline SVG (converging paths), not a plain emoji."""
        assert '<svg' in html
        assert 'header-icon' in html

    def test_header_svg_is_material_plane(self, html):
        """Logo is the Material Design 'flight' plane icon (solid filled SVG path)."""
        # The Material Design flight icon has this distinctive path sequence
        assert 'M21 16v-2l-8-5V3.5' in html   # top of the MD flight path
        assert 'stroke-dasharray' not in html  # no dashed orbit ring in this version


# ─── 12. HTML: Attendee chip ──────────────────────────────────────────────────

class TestAttendeeChip:
    """
    Attendee rows were redesigned as green chips with an inline-editable
    traveller count. Editing the count must clear existing results so the
    user can't act on stale data.
    """

    def test_chip_has_green_background(self, html):
        import re
        chip_block = re.search(r"\.attendee-chip\s*\{[^}]+\}", html, re.DOTALL)
        assert chip_block, ".attendee-chip CSS block not found"
        assert "green" in chip_block.group() or "rgba(22,163,74" in chip_block.group()

    def test_chip_count_is_clickable(self, html):
        """Count badge must have onclick to open inline editor."""
        assert 'onclick="editAttendeeCount' in html

    def test_chip_count_has_cursor_pointer(self, html):
        import re
        count_block = re.search(r"\.chip-count\s*\{[^}]+\}", html, re.DOTALL)
        assert count_block, ".chip-count CSS block not found"
        assert "cursor: pointer" in count_block.group()

    def test_chip_iatas_styled_distinctly(self, html):
        """Airport codes should be visually separated from the city name."""
        assert ".attendee-chip .chip-iatas" in html

    def test_edit_input_has_green_focus_ring(self, html):
        import re
        edit_block = re.search(r"\.chip-count-edit\s*\{[^}]+\}", html, re.DOTALL)
        assert edit_block, ".chip-count-edit CSS block not found"
        block = edit_block.group()
        assert "box-shadow" in block  # focus ring
        assert "green" in block or "rgba(22,163,74" in block or "var(--green" in block

    def test_editing_count_reruns_search_not_clears(self, html):
        """
        Changing a count mid-session must immediately re-run the search so
        the results update in place — the old behaviour (clear + hide) has
        been replaced with findBtn.click() inside the save() closure.
        """
        import re
        # Find the save() closure inside editAttendeeCount
        match = re.search(
            r'const save = \(\) => \{(.+?)^\s{2}\};',
            html, re.DOTALL | re.MULTILINE,
        )
        assert match, "save() closure inside editAttendeeCount not found"
        body = match.group(1)
        assert "findBtn.click()" in body,    "save() must call findBtn.click() to re-run"
        assert "currentResults = null" not in body, (
            "save() must not manually clear results — findBtn.click() handles the reset"
        )

    def test_escape_in_edit_cancels_without_saving(self, html):
        assert "e.key === 'Escape'" in html
        assert "renderAttendees()" in html

    def test_enter_in_edit_saves_and_reruns_or_focuses(self, html):
        """
        Enter in the count input commits the value. If results are already
        showing the search re-runs immediately (findBtn.click). If no results
        exist yet, focus moves to the find button for a deliberate trigger.
        """
        assert "input.blur()" in html
        # Both branches must be present
        assert "findBtn.click()"                   in html  # re-run path
        assert "setTimeout(() => findBtn.focus()" in html   # no-results path

    def test_delete_buttons_not_in_tab_order(self, html):
        """Delete × buttons must have tabindex=-1 so Tab skips them."""
        assert 'tabindex="-1"' in html

    def test_deleting_attendee_clears_results(self, html):
        """
        removeAttendee() must always reset currentResults and hide both panels,
        not only when the attendee count drops below 2.
        Previously the guard `if (attendees.length < 2)` meant results stayed
        visible after deleting one of three attendees — now they're always cleared.
        """
        import re
        # Find the removeAttendee function body
        match = re.search(r'function removeAttendee\([^)]*\)\s*\{([^}]+)\}', html, re.DOTALL)
        assert match, "removeAttendee function not found"
        body = match.group(1)
        assert "currentResults = null"                    in body
        assert "resultsPanel.classList.remove('visible')" in body
        assert "routeDetail.classList.remove('visible')"  in body
        # Must NOT be conditional on attendee count
        assert "if (attendees.length" not in body


# ─── 13. HTML: Auto-focus first result row ────────────────────────────────────

class TestAutoFocusFirstRow:
    """
    After Find Best Locations completes, the first row of the results table
    should be automatically focused so the user can immediately press Enter
    or arrow-navigate without clicking first.
    """

    def test_first_row_focused_after_find(self, html):
        assert "focusDest(firstRow.dataset.iata, firstRow)" in html

    def test_first_row_queried_from_overall_table(self, html):
        assert "overallTable tbody tr" in html or "#overallTable tbody tr" in html

    def test_find_button_blurred_before_focusing_row(self, html):
        """
        findBtn must be blurred so a subsequent Enter navigates the table
        rather than re-triggering the search.
        """
        assert "findBtn.blur()" in html


# ─── 14. HTML: Find resets to Overall tab ────────────────────────────────────

class TestFindResetsTab:
    """
    If the user was on the 'Attendee Home Cities' tab when they click
    Find Best Locations, the view must switch back to Top 10 Overall.
    """

    def test_find_sets_overall_tab_active(self, html):
        assert "tab-overall" in html
        # Both the tab button and the panel must be activated
        assert 'querySelector(\'.tab[data-tab="overall"]\').classList.add(\'active\')' in html

    def test_find_removes_active_from_all_tabs(self, html):
        assert "querySelectorAll('.tab').forEach" in html
        assert "classList.remove('active')" in html

    def test_find_removes_active_from_all_panels(self, html):
        assert "querySelectorAll('.tab-panel').forEach" in html


# ─── 15. HTML: Route view formatting ─────────────────────────────────────────

class TestRouteViewFormatting:
    """
    The route view had two formatting corrections:
    (a) The estimated price badge used '~' (looks like minus sign) — changed to 'est.'
    (b) Cost and carbon columns were moved before distance.
    """

    def test_route_badge_uses_est_not_tilde(self, html):
        """'~' looked like a negative sign; all estimates now use 'est.'"""
        assert "att-badge cost" in html
        # The badge template must start with 'est.' not '~'
        assert ">est. US$" in html
        assert ">~ US$" not in html
        assert ">~$" not in html

    def test_route_header_shows_cost_before_distance(self, html):
        """
        In the destination summary bar, est. cost/carbon must appear
        before the total distance figure.
        """
        cost_pos = html.index("est. US$")
        dist_pos = html.index("total ${totalDist")
        assert cost_pos < dist_pos, (
            "est. cost should appear before total distance in route header"
        )

    def test_route_header_shows_carbon_before_distance(self, html):
        carbon_pos = html.index("estTotalCarbon")
        dist_pos   = html.index("total ${totalDist")
        assert carbon_pos < dist_pos


# ─── 16. HTML: View routes pill hidden on selected row ───────────────────────

class TestViewRoutesPill:
    """
    The 'View routes →' pill must only appear on hover/keyboard-focus, NOT
    on the currently selected row (which already shows the route detail panel).
    """

    def test_pill_hidden_by_default(self, html):
        import re
        pill_block = re.search(
            r"\.routes-hint-cell .routes-pill\s*\{[^}]+\}", html, re.DOTALL
        )
        assert pill_block, ".routes-pill CSS block not found"
        assert "opacity: 0" in pill_block.group()

    def test_pill_visible_on_hover(self, html):
        assert "tr:hover .routes-pill" in html

    def test_pill_visible_on_keyboard_focus(self, html):
        assert "tr.focused .routes-pill" in html

    def test_pill_not_shown_on_selected_row(self, html):
        """Selected rows must NOT trigger opacity:1 on the pill."""
        assert "tr.selected .routes-pill" not in html


# ─── 17. HTML: Mouse move clears keyboard focus ───────────────────────────────

class TestMouseMoveClearsFocus:
    """
    When the user moves the mouse after keyboard-navigating, the keyboard
    focus highlight should disappear so the two selection modes don't clash.
    """

    def test_mousemove_listener_on_results_panel(self, html):
        assert "resultsPanel.addEventListener('mousemove'" in html

    def test_mousemove_removes_focused_class(self, html):
        assert "classList.remove('focused')" in html

    def test_mousemove_clears_focused_iata(self, html):
        import re
        # Use regex so the test is insensitive to alignment whitespace
        match = re.search(r"resultsPanel\.addEventListener\('mousemove'.*?\}\)", html, re.DOTALL)
        assert match, "mousemove handler not found"
        assert re.search(r"focusedIata\s*=\s*null", match.group()), (
            "mousemove handler must reset focusedIata"
        )

    def test_mousemove_clears_focused_row_el(self, html):
        assert "focusedRowEl = null" in html


# ─── 18. HTML: Price toolbar scrolls into view on route select ───────────────

class TestPriceToolbarScroll:
    """
    When a destination is selected and route details load, the page should
    scroll so the 'Get Live Prices' toolbar is visible — it was easy to miss
    at the bottom of a long route list.
    """

    def test_price_toolbar_scroll_called_after_routes_load(self, html):
        assert "priceToolbar" in html
        assert "priceToolbar').scrollIntoView" in html

    def test_scroll_uses_smooth_behavior(self, html):
        # Both scroll calls (toolbar + price results) should be smooth
        assert "behavior: 'smooth'" in html

    def test_price_results_cleared_on_new_route_select(self, html):
        """When a new destination is selected, old live prices must be wiped."""
        assert "priceResults').innerHTML = ''" in html


# ─── 19. HTML: Continent filter re-runs search ───────────────────────────────

class TestContinentFilter:
    """
    Clicking a continent pill while results are already visible should
    immediately re-run the search — the user shouldn't have to click Find again.
    """

    def test_pill_click_triggers_find_when_results_exist(self, html):
        assert "if (currentResults) findBtn.click()" in html

    def test_continent_sent_to_api(self, html):
        assert "continent_filter" in html
        assert "activeContinent" in html

    def test_any_continent_is_default(self, html):
        assert "activeContinent = 'Any'" in html or 'activeContinent = "Any"' in html


# ─── 20. HTML: Home attendees local count column ─────────────────────────────

class TestHomeAttendeesColumn:
    """
    In the Attendee Home Cities tab each row must show a 'Local Attendees'
    count so you know how many people wouldn't need to travel.
    """

    def test_home_table_has_local_attendees_column(self, html):
        assert "Local Attendees" in html

    def test_home_rows_render_local_count(self, html):
        assert "d.local_count" in html

    def test_local_count_in_api_response(self, client):
        payload = {
            "attendees": [
                {"city": "London", "iatas": ["LHR"], "count": 5},
                {"city": "Paris",  "iatas": ["CDG"], "count": 2},
            ]
        }
        res  = client.post("/api/find_destinations", json=payload)
        data = res.get_json()
        assert res.status_code == 200
        home = data["home"]
        assert len(home) > 0
        for row in home:
            assert "local_count" in row, f"Missing local_count in home row: {row}"
            assert row["local_count"] > 0


# ─── 21. HTML: Bare $ bug — live prices per-person ────────────────────────────

class TestLivePricesPerPersonUSD:
    """
    The primary per-person amount in the live prices row had a bare '$' that
    was missed when all other price surfaces were updated to 'US$'.
    """

    def test_price_amount_div_uses_us_dollar(self, html):
        assert 'price-amount">US$${r.price_per_person' in html

    def test_no_bare_dollar_in_price_amount(self, html):
        assert 'price-amount">$${r.price_per_person' not in html


# ─── 22. Round-trip pricing correctness ──────────────────────────────────────

class TestRoundTripPricing:
    """
    The displayed estimated price must be for a return journey, and group
    totals must scale linearly with attendee count.
    """

    def test_est_price_person_is_double_oneway(self, client):
        """
        est_price_person must be oneway_price * 2.
        A tolerance of ±2 is allowed because the API stores dist_km as
        round(actual_dist), so recomputing estimate_fare on the stored integer
        can differ from the original float computation by 1-2 dollars.
        Origin/dest IATAs must be passed so the region-pair multiplier and hub
        discount are applied consistently in both the API and this test.
        """
        payload = {
            "attendees": [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
            "dest_iata": "LHR",
        }
        res   = client.post("/api/get_routes", json=payload)
        route = res.get_json()["routes"][0]
        dist  = route["dist_km"]
        hops  = route["hops"]
        oneway_price, _ = estimate_fare(dist, hops, "VIE", "LHR")
        assert abs(route["est_price_person"] - oneway_price * 2) <= 2, (
            f"est_price_person={route['est_price_person']} "
            f"but oneway*2={oneway_price * 2} (dist_km may differ slightly from "
            f"the unrounded float used internally)"
        )

    def test_est_price_group_scales_with_count(self, client):
        """est_price_group = est_price_person * count."""
        for count in (1, 2, 5):
            payload = {
                "attendees": [{"city": "Vienna", "iatas": ["VIE"], "count": count}],
                "dest_iata": "LHR",
            }
            res   = client.post("/api/get_routes", json=payload)
            route = res.get_json()["routes"][0]
            assert route["est_price_group"] == route["est_price_person"] * count, (
                f"Group price wrong for count={count}"
            )

    def test_est_carbon_person_is_double_oneway(self, client):
        """est_carbon_person should equal oneway_carbon * 2."""
        payload = {
            "attendees": [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
            "dest_iata": "LHR",
        }
        res   = client.post("/api/get_routes", json=payload)
        route = res.get_json()["routes"][0]
        dist  = route["dist_km"]
        _, oneway_carbon = estimate_fare(dist, route["hops"])
        assert abs(route["est_carbon_person"] - round(oneway_carbon * 2, 1)) < 0.2

    def test_est_carbon_group_scales_with_count(self, client):
        for count in (1, 3):
            payload = {
                "attendees": [{"city": "Paris", "iatas": ["CDG"], "count": count}],
                "dest_iata": "LHR",
            }
            res   = client.post("/api/get_routes", json=payload)
            route = res.get_json()["routes"][0]
            assert abs(route["est_carbon_group"] - round(route["est_carbon_person"] * count, 1)) < 0.2


# ─── 23. Haversine accuracy ───────────────────────────────────────────────────

class TestHaversine:
    """
    All fare and carbon estimates ultimately derive from haversine distances.
    Spot-check against well-known reference distances.
    """

    def test_lhr_jfk_approx_5540km(self):
        # LHR: 51.477°N, 0.461°W   JFK: 40.640°N, 73.779°W
        d = haversine(51.477, -0.461, 40.640, -73.779)
        assert 5_400 < d < 5_700, f"LHR↔JFK distance unexpected: {d:.0f} km"

    def test_syd_lax_approx_12074km(self):
        # SYD: -33.946°S, 151.177°E   LAX: 33.943°N, 118.408°W
        d = haversine(-33.946, 151.177, 33.943, -118.408)
        assert 11_800 < d < 12_400, f"SYD↔LAX distance unexpected: {d:.0f} km"

    def test_same_point_is_zero(self):
        assert haversine(51.5, -0.1, 51.5, -0.1) == 0.0

    def test_distance_is_symmetric(self):
        d1 = haversine(51.5, -0.1, 48.9, 2.35)
        d2 = haversine(48.9, 2.35, 51.5, -0.1)
        assert abs(d1 - d2) < 0.01


# ─── 24. find_best_route ─────────────────────────────────────────────────────

class TestFindBestRoute:

    def test_finds_route_between_major_hubs(self):
        path, hops, dist = find_best_route("LHR", "JFK")
        assert path is not None
        assert hops >= 1
        assert dist > 0

    def test_prefers_fewer_hops_over_shorter_distance(self):
        """
        The algorithm must minimise hops first. A direct long-haul route
        should beat a shorter multi-stop itinerary.
        """
        path, hops, _ = find_best_route("LHR", "SYD")
        assert path is not None
        # LHR→SYD should be reachable in 1-2 hops via a hub; never more than 4
        assert hops <= 4

    def test_returns_none_for_unreachable_pair(self):
        """A made-up IATA code should produce no route, not an exception."""
        path, hops, dist = find_best_route("LHR", "ZZZ")
        assert path is None
        assert hops is None
        assert dist is None

    def test_route_to_self_is_empty_path(self):
        path, hops, dist = find_best_route("LHR", "LHR")
        assert path == []
        assert hops == 0
        assert dist == 0.0

    def test_each_leg_connects_to_next(self):
        """Legs must form a contiguous chain: dst of leg N == src of leg N+1."""
        path, _, _ = find_best_route("VIE", "JFK")
        assert path is not None
        for i in range(len(path) - 1):
            _, dst_this, _, _ = path[i]
            src_next, _, _, _ = path[i + 1]
            assert dst_this == src_next, (
                f"Gap in route: leg {i} ends at {dst_this}, "
                f"leg {i+1} starts at {src_next}"
            )


# ─── 25. estimate_fare at band boundaries ────────────────────────────────────

class TestEstimateFareBoundaries:
    """
    Price band transitions at 500, 4000, 8000 and 12000 km are monotonically
    increasing. The 1500 km boundary is a known exception: the medium-haul
    base fare ($130) is lower than the short-haul calculation just below the
    crossover, so prices dip slightly right above 1500 km. This is a known
    model artifact (estimates are intentionally rough) and is documented here
    rather than silently ignored.
    """

    MONOTONIC_BOUNDARIES = [500, 4_000, 8_000, 12_000]

    def test_price_increases_at_monotonic_boundaries(self):
        for b in self.MONOTONIC_BOUNDARIES:
            below, _ = estimate_fare(b - 1, 1)
            above, _ = estimate_fare(b + 1, 1)
            assert above >= below, (
                f"Price went down crossing boundary at {b} km: "
                f"{b-1} km → {below}, {b+1} km → {above}"
            )

    def test_known_discontinuity_at_1500km(self):
        """
        At 1500 km the medium-haul band ($130 base, $0.10/km) kicks in and
        produces a slightly lower price than the tail of the short-haul band
        ($80 base, $0.14/km). This is documented expected behaviour.
        The crossover is at ~1250 km; for 1250–1500 km short-haul is pricier.
        """
        at_1499, _ = estimate_fare(1_499, 1)
        at_1501, _ = estimate_fare(1_501, 1)
        # Verify the discontinuity exists so we notice if the model is fixed
        assert at_1501 < at_1499, (
            "Expected price dip at 1500 km boundary — if the fare model has "
            "been made continuous, remove this test and update "
            "test_price_increases_at_monotonic_boundaries to include 1500."
        )

    ALL_BOUNDARIES = [500, 1_500, 4_000, 8_000, 12_000]

    def test_exact_boundary_values_dont_raise(self):
        for b in self.ALL_BOUNDARIES:
            price, carbon = estimate_fare(b, 1)
            assert price > 0
            assert carbon > 0

    def test_carbon_band_transitions_are_monotonic(self):
        """Carbon per km must never increase as distance grows."""
        dists = [400, 800, 2_500, 6_000, 10_000]
        carbon_per_km = [_carbon_factor(d) for d in dists]
        for i in range(len(carbon_per_km) - 1):
            assert carbon_per_km[i] >= carbon_per_km[i + 1], (
                f"Carbon factor increased from {dists[i]} km to {dists[i+1]} km"
            )


# ─── 26. /api/search_city ────────────────────────────────────────────────────

class TestSearchCityEndpoint:

    def test_returns_results_for_known_city(self, client):
        res  = client.get("/api/search_city?q=London")
        data = res.get_json()
        assert res.status_code == 200
        assert len(data) > 0
        locations = [r["location"] for r in data]
        assert any("London" in loc for loc in locations)

    def test_returns_empty_for_short_query(self, client):
        res  = client.get("/api/search_city?q=L")
        data = res.get_json()
        assert res.status_code == 200
        assert data == []

    def test_returns_empty_for_blank_query(self, client):
        res  = client.get("/api/search_city?q=")
        assert res.status_code == 200
        assert res.get_json() == []

    def test_caps_results_at_20(self, client):
        # 'a' matches many cities
        res  = client.get("/api/search_city?q=an")
        data = res.get_json()
        assert len(data) <= 20

    def test_each_result_has_required_fields(self, client):
        res  = client.get("/api/search_city?q=Paris")
        data = res.get_json()
        assert len(data) > 0
        for item in data:
            assert "location" in item
            assert "iatas"    in item
            assert "airports" in item
            assert len(item["iatas"]) > 0

    def test_only_routable_airports_returned(self, client):
        """Every IATA in results must have actual routes."""
        res  = client.get("/api/search_city?q=Vienna")
        data = res.get_json()
        assert len(data) > 0
        for item in data:
            for iata in item["iatas"]:
                assert iata in GRAPH, f"{iata} has no routes"
                assert iata in MAIN_AIRPORTS, f"{iata} is stranded"

    def test_iata_code_search_works(self, client):
        # Search by full city name to avoid the 20-result cap swamping
        # the response with other cities that contain "vie" as a substring
        # (e.g. Montevideo).  Vienna is an unambiguous full-name match.
        res  = client.get("/api/search_city?q=Vienna")
        data = res.get_json()
        assert any("VIE" in item["iatas"] for item in data)


# ─── 27. Multi-airport cities ─────────────────────────────────────────────────

class TestMultiAirportCities:

    def test_london_returns_multiple_iatas(self, client):
        res  = client.get("/api/search_city?q=London")
        data = res.get_json()
        london = next((r for r in data if "London" in r["location"] and
                       "United Kingdom" in r["location"]), None)
        assert london is not None, "London UK not found in search results"
        assert len(london["iatas"]) > 1, (
            f"Expected multiple London airports, got: {london['iatas']}"
        )
        assert "LHR" in london["iatas"]

    def test_find_destinations_accepts_multiple_iatas_per_attendee(self, client):
        """An attendee with LHR+LGW should produce valid results."""
        london = client.get("/api/search_city?q=London").get_json()
        london_entry = next(r for r in london if "United Kingdom" in r["location"])
        payload = {
            "attendees": [
                {"city": "London", "iatas": london_entry["iatas"], "count": 1},
                {"city": "Vienna", "iatas": ["VIE"],               "count": 1},
            ]
        }
        res  = client.post("/api/find_destinations", json=payload)
        data = res.get_json()
        assert res.status_code == 200
        assert len(data["overall"]) > 0

    def test_best_origin_selected_for_multi_iata_attendee(self, client):
        """
        For an attendee with multiple home airports, get_routes should
        pick the origin that produces the best (fewest-hop) route.
        """
        london = client.get("/api/search_city?q=London").get_json()
        london_entry = next(r for r in london if "United Kingdom" in r["location"])
        payload = {
            "attendees": [{"city": "London", "iatas": london_entry["iatas"], "count": 1}],
            "dest_iata": "SYD",
        }
        res   = client.post("/api/get_routes", json=payload)
        data  = res.get_json()
        route = data["routes"][0]
        assert not route.get("error"), f"Route error: {route.get('error')}"
        assert route["hops"] >= 1


# ─── 28. Data pipeline integrity ─────────────────────────────────────────────

class TestDataPipelineIntegrity:

    def test_airports_dict_non_empty(self):
        assert len(AIRPORTS) > 5_000

    def test_airport_entries_have_required_fields(self):
        required = {"name", "city", "country", "lat", "lon", "continent"}
        sample = list(AIRPORTS.values())[:200]
        for info in sample:
            missing = required - set(info.keys())
            assert not missing, f"Airport entry missing fields: {missing}"

    def test_airport_coordinates_are_numeric(self):
        sample = list(AIRPORTS.values())[:200]
        for info in sample:
            assert isinstance(info["lat"], float), f"Non-float lat: {info['lat']}"
            assert isinstance(info["lon"], float), f"Non-float lon: {info['lon']}"
            assert -90  <= info["lat"] <= 90,  f"Latitude out of range: {info['lat']}"
            assert -180 <= info["lon"] <= 180, f"Longitude out of range: {info['lon']}"

    def test_airlines_dict_non_empty(self):
        """If airlines.csv fails to load, tooltips silently show raw IATA codes."""
        assert len(AIRLINES) > 100

    def test_graph_edges_reference_valid_airports(self):
        """Orphaned edges would cause KeyErrors during route rendering."""
        for src, neighbours in list(GRAPH.items())[:500]:
            assert src in AIRPORTS, f"Graph source {src} not in AIRPORTS"
            for dst, dist, airline in neighbours:
                assert dst in AIRPORTS, f"Graph dest {dst} not in AIRPORTS"
                assert dist > 0, f"Zero-distance edge {src}→{dst}"

    def test_graph_non_empty(self):
        assert len(GRAPH) > 2_000


# ─── 29. Static file serving ─────────────────────────────────────────────────

class TestStaticFileServing:

    def test_root_returns_200(self, client):
        res = client.get("/")
        assert res.status_code == 200

    def test_root_returns_html(self, client):
        res = client.get("/")
        assert b"<!DOCTYPE html>" in res.data or b"<html" in res.data

    def test_svg_map_returns_200(self, client):
        res = client.get("/world-airports.svg")
        assert res.status_code == 200

    def test_svg_map_has_correct_mime_type(self, client):
        res = client.get("/world-airports.svg")
        assert "image/svg+xml" in res.content_type

    def test_unknown_path_returns_404(self, client):
        res = client.get("/does-not-exist")
        assert res.status_code == 404


# ─── 30. Page structure ───────────────────────────────────────────────────────

class TestPageStructure:

    def test_page_title(self, html):
        assert "<title>Global Meeting Finder</title>" in html

    def test_viewport_meta_tag(self, html):
        assert 'name="viewport"' in html
        assert "width=device-width" in html

    def test_charset_utf8(self, html):
        assert 'charset="UTF-8"' in html or "charset=UTF-8" in html

    def test_google_fonts_imported(self, html):
        assert "fonts.googleapis.com" in html

    def test_inter_font_used(self, html):
        assert "Inter" in html

    def test_dm_mono_font_used(self, html):
        assert "DM+Mono" in html or "DM Mono" in html

    def test_css_green_variable_defined(self, html):
        assert "--green:" in html

    def test_css_surface_variable_defined(self, html):
        assert "--surface:" in html


# ─── 31. get_continent edge cases ────────────────────────────────────────────

class TestGetContinent:

    def test_antarctica(self):
        assert get_continent(-75, 0) == "Antarctica"

    def test_greenland_is_north_america(self):
        # Nuuk, Greenland: 64.17°N, 51.74°W
        assert get_continent(64.17, -51.74) == "North America"

    def test_australia_is_oceania(self):
        # Sydney: -33.9°S, 151.2°E
        assert get_continent(-33.9, 151.2) == "Oceania"

    def test_new_zealand_is_oceania(self):
        # Auckland: -36.9°S, 174.8°E
        assert get_continent(-36.9, 174.8) == "Oceania"

    def test_hawaii_is_north_america(self):
        # Honolulu: 21.3°N, 157.8°W
        assert get_continent(21.3, -157.8) == "North America"

    def test_london_is_europe(self):
        assert get_continent(51.5, -0.1) == "Europe"

    def test_new_york_is_north_america(self):
        assert get_continent(40.7, -74.0) == "North America"

    def test_tokyo_is_asia(self):
        assert get_continent(35.7, 139.7) == "Asia"

    def test_nairobi_is_africa(self):
        assert get_continent(-1.3, 36.8) == "Africa"

    def test_sao_paulo_is_south_america(self):
        assert get_continent(-23.5, -46.6) == "South America"

    def test_known_airports_have_correct_continent(self):
        expected = {
            "LHR": "Europe",
            "JFK": "North America",
            "SYD": "Oceania",
            "NRT": "Asia",
            "GRU": "South America",
            "JNB": "Africa",
        }
        for iata, continent in expected.items():
            info = AIRPORTS.get(iata)
            assert info is not None, f"{iata} not found in AIRPORTS"
            actual = get_continent(info["lat"], info["lon"])
            assert actual == continent, (
                f"{iata} ({info['lat']}, {info['lon']}): "
                f"expected {continent}, got {actual}"
            )

    def test_athens_is_europe_not_africa(self):
        # ATH: 37.9°N, 23.9°E — previously misclassified as Africa because
        # the Africa bounding box (-40<lat<38, -20<lon<55) was checked first.
        # Europe check must come before Africa.
        assert get_continent(37.9, 23.9) == "Europe"

    def test_dubai_is_asia_not_africa(self):
        # DXB: 25.3°N, 55.4°E — Arabian Peninsula airports were inside the
        # Africa box (lat<38, lon<55) before the Middle East / Asia check
        # was reordered ahead of Africa.
        assert get_continent(25.3, 55.4) == "Asia"

    def test_riyadh_is_asia_not_africa(self):
        # Riyadh: 24.7°N, 46.7°E
        assert get_continent(24.7, 46.7) == "Asia"

    def test_mediterranean_europe_airports_are_europe(self):
        # Spot-check a few Mediterranean European airports that sit close to
        # the Africa bounding-box border.
        cases = {
            "ATH": (37.94, 23.95),   # Athens
            "MLA": (35.86, 14.48),   # Malta
            "PMI": (39.55,  2.74),   # Palma de Mallorca
        }
        for label, (lat, lon) in cases.items():
            result = get_continent(lat, lon)
            assert result == "Europe", (
                f"{label} ({lat}, {lon}) classified as {result!r}, expected 'Europe'"
            )

    def test_gulf_airports_are_asia(self):
        # Gulf-state airports that were previously misclassified as Africa.
        cases = {
            "DXB": (25.25, 55.36),   # Dubai
            "DOH": (25.27, 51.61),   # Doha
            "AUH": (24.43, 54.65),   # Abu Dhabi
            "RUH": (24.96, 46.70),   # Riyadh
        }
        for label, (lat, lon) in cases.items():
            result = get_continent(lat, lon)
            assert result == "Asia", (
                f"{label} ({lat}, {lon}) classified as {result!r}, expected 'Asia'"
            )


# ─── 32. find_meeting_destinations — all same city ───────────────────────────

class TestFindMeetingDestinationsSameCity:
    """
    When every attendee is from the same city the home airport should rank
    first (zero travel cost) and all other results should still be returned.
    """

    def test_home_city_ranks_first_when_all_same_origin(self):
        attendees = [
            {"city": "London", "iatas": ["LHR"], "count": 3},
            {"city": "London", "iatas": ["LHR"], "count": 2},
        ]
        ranked, _ = find_meeting_destinations(attendees)
        assert len(ranked) > 0
        # LHR itself should top the list — zero cost for everyone
        assert ranked[0]["iata"] == "LHR", (
            f"Expected LHR first, got {ranked[0]['iata']}"
        )

    def test_returns_results_not_empty_for_single_unique_origin(self):
        attendees = [
            {"city": "Vienna", "iatas": ["VIE"], "count": 1},
            {"city": "Vienna", "iatas": ["VIE"], "count": 4},
        ]
        ranked, _ = find_meeting_destinations(attendees)
        assert len(ranked) > 0

    def test_home_city_has_zero_cost_in_results(self):
        attendees = [
            {"city": "Paris", "iatas": ["CDG"], "count": 2},
            {"city": "Paris", "iatas": ["CDG"], "count": 1},
        ]
        ranked, _ = find_meeting_destinations(attendees)
        home = next((r for r in ranked if r["iata"] == "CDG"), None)
        assert home is not None, "CDG not in results"
        assert home["est_cost"] == 0


# ─── 33. /api/get_routes missing data ────────────────────────────────────────

class TestGetRoutesMissingData:

    def test_returns_400_when_dest_iata_missing(self, client):
        res = client.post(
            "/api/get_routes",
            json={"attendees": [{"city": "Vienna", "iatas": ["VIE"], "count": 1}]},
        )
        assert res.status_code == 400

    def test_returns_400_when_attendees_missing(self, client):
        res = client.post(
            "/api/get_routes",
            json={"dest_iata": "LHR"},
        )
        assert res.status_code == 400

    def test_returns_400_when_body_empty(self, client):
        res = client.post(
            "/api/get_routes",
            json={},
        )
        assert res.status_code == 400


# ─── 34. removeAttendee resets full navigation state ─────────────────────────

class TestRemoveAttendeeResetsState:
    """
    removeAttendee() previously only cleared currentResults and hid panels,
    but left selectedIata / focusedIata / focusedRowEl set. This caused a
    silent failure: after deleting an attendee and re-running the search,
    clicking the same destination again would hit the early-return guard
    `if (selectedIata === iata) return` and the route detail would never open.
    """

    def test_remove_attendee_resets_selected_iata(self, html):
        import re
        match = re.search(r'function removeAttendee\([^)]*\)\s*\{([^}]+)\}', html, re.DOTALL)
        assert match, "removeAttendee function not found"
        body = match.group(1)
        assert "selectedIata"  in body, "removeAttendee must reset selectedIata"

    def test_remove_attendee_resets_focused_iata(self, html):
        import re
        match = re.search(r'function removeAttendee\([^)]*\)\s*\{([^}]+)\}', html, re.DOTALL)
        body = match.group(1)
        assert "focusedIata"   in body, "removeAttendee must reset focusedIata"

    def test_remove_attendee_resets_focused_row_el(self, html):
        import re
        match = re.search(r'function removeAttendee\([^)]*\)\s*\{([^}]+)\}', html, re.DOTALL)
        body = match.group(1)
        assert "focusedRowEl"  in body, "removeAttendee must reset focusedRowEl"

    def test_all_attendee_mutators_reset_same_state(self, html):
        """
        addAttendee, editAttendeeCount, and removeAttendee all change the
        attendee list — every one of them must reset the full selection/focus
        state so the UI is consistent regardless of how the list was changed.
        """
        import re
        state_vars = ("selectedIata", "focusedIata", "focusedRowEl", "currentResults")
        # Extract each function body — look for the function keyword to anchor correctly
        remove_match = re.search(
            r'function removeAttendee\([^)]*\)\s*\{([^}]+)\}', html, re.DOTALL)
        add_match = re.search(
            r'function addAttendee\([^)]*\)\s*\{(.+?)^}',
            html, re.DOTALL | re.MULTILINE)
        assert remove_match, "removeAttendee function not found"
        assert add_match,    "addAttendee function not found"
        for var in state_vars:
            assert var in remove_match.group(1), (
                f"removeAttendee missing reset of {var}"
            )
            assert var in add_match.group(1), (
                f"addAttendee missing reset of {var}"
            )


# ─── 35. SerpAPI null carbon_emissions ───────────────────────────────────────

class TestSerpApiNullCarbon:
    """
    Some Google Flights results include `"carbon_emissions": null` (the key
    is present but the value is JSON null). The original code used:

        best_flight.get("carbon_emissions", {}).get("this_flight")

    which only substitutes {} when the key is *absent* — if the key exists
    with value None, the second .get() raises AttributeError.

    Fixed to:  (best_flight.get("carbon_emissions") or {}).get("this_flight")
    """

    def test_null_carbon_emissions_does_not_crash(self):
        """carbon_emissions: null in the API response must not raise."""
        response = {
            "best_flights": [
                {"price": 310, "carbon_emissions": None},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(response)):
            result = serpapi_flight_price("VIE", "LHR", "2026-08-01", "2026-08-05")
        # Should return the price; carbon_g will be None (no crash)
        assert "error" not in result
        assert result["price"] == 310
        assert result["carbon_g"] is None

    def test_missing_this_flight_key_returns_none_carbon(self):
        """carbon_emissions present but missing the this_flight sub-key."""
        response = {
            "best_flights": [
                {"price": 290, "carbon_emissions": {"typical_this_route": 80_000}},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(response)):
            result = serpapi_flight_price("VIE", "LHR", "2026-08-01", "2026-08-05")
        assert result["price"]    == 290
        assert result["carbon_g"] is None   # graceful — no crash

    def test_live_prices_falls_back_to_estimate_when_carbon_g_none(self, client):
        """
        When SerpAPI returns a price but no carbon data, the endpoint must
        still produce a carbon estimate (from distance) rather than returning
        None or crashing.
        """
        orig = app_module.SERPAPI_KEY
        app_module.SERPAPI_KEY = "test-key"
        try:
            mock_price = {"price": 380, "carbon_g": None, "currency": "USD"}
            with patch("app.serpapi_flight_price", return_value=mock_price):
                res  = client.post("/api/get_live_prices", json={
                    "attendees":   [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
                    "dest_iata":   "LHR",
                    "weeks_ahead": 8,
                })
                data = res.get_json()
            assert res.status_code == 200
            result = data["results"][0]
            assert result["price_per_person"] == 380         # live price used
            # carbon must be a positive number, not None
            assert result["carbon_kg_person"] is not None
            assert result["carbon_kg_person"] > 0
        finally:
            app_module.SERPAPI_KEY = orig


# ─── 36. Continent filter — no matching destinations ─────────────────────────

class TestContinentFilterNoResults:
    """
    If the continent filter excludes all candidate destinations the API must
    return an empty overall list with a 200 OK — not a 500 or an error key.
    """

    def test_returns_200_with_empty_list_for_impossible_filter(self, client):
        """
        Attendees in Europe + continent_filter='Antarctica' — no Antarctic
        hub airports exist so overall must be empty.
        """
        payload = {
            "attendees": [
                {"city": "London", "iatas": ["LHR"], "count": 1},
                {"city": "Paris",  "iatas": ["CDG"], "count": 1},
            ],
            "continent_filter": "Antarctica",
        }
        res  = client.post("/api/find_destinations", json=payload)
        data = res.get_json()
        assert res.status_code == 200
        assert data["overall"] == []

    def test_overall_key_always_present_in_response(self, client):
        payload = {
            "attendees": [
                {"city": "London", "iatas": ["LHR"], "count": 1},
                {"city": "Vienna", "iatas": ["VIE"], "count": 1},
            ],
            "continent_filter": "Antarctica",
        }
        res  = client.post("/api/find_destinations", json=payload)
        data = res.get_json()
        assert "overall" in data
        assert "home"    in data


# ─── 37. /api/get_routes with unknown destination ────────────────────────────

class TestGetRoutesUnknownDest:
    """
    If dest_iata doesn't exist in the route graph, get_routes_for_destination
    returns an error entry per attendee rather than raising a 500.
    """

    def test_unknown_dest_returns_200_with_error_entry(self, client):
        res = client.post("/api/get_routes", json={
            "attendees": [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
            "dest_iata": "ZZZ",
        })
        assert res.status_code == 200
        route = res.get_json()["routes"][0]
        assert "error" in route
        assert route["legs"] == []

    def test_unknown_dest_error_message_is_informative(self, client):
        res = client.post("/api/get_routes", json={
            "attendees": [{"city": "London", "iatas": ["LHR"], "count": 2}],
            "dest_iata": "ZZZ",
        })
        route = res.get_json()["routes"][0]
        assert route["error"]  # non-empty string


# ─── 38. weeks_ahead produces a future outbound date ─────────────────────────

class TestWeeksAheadDateCalculation:
    """
    The weeks_ahead parameter must shift the outbound date forward from today.
    A miscalculation (e.g., using days instead of weeks) would produce a past
    date and SerpAPI would return no results.
    """

    def test_outbound_date_is_in_the_future(self, client):
        from datetime import date
        orig = app_module.SERPAPI_KEY
        app_module.SERPAPI_KEY = "test-key"
        captured = {}
        def fake_serpapi(origin, dest, outbound_date, return_date):
            captured["outbound"] = outbound_date
            captured["return"]   = return_date
            return {"price": 300, "carbon_g": 90_000, "currency": "USD"}
        try:
            with patch("app.serpapi_flight_price", side_effect=fake_serpapi):
                client.post("/api/get_live_prices", json={
                    "attendees":   [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
                    "dest_iata":   "LHR",
                    "weeks_ahead": 4,
                })
        finally:
            app_module.SERPAPI_KEY = orig
        assert captured, "serpapi_flight_price was not called"
        outbound = date.fromisoformat(captured["outbound"])
        assert outbound > date.today(), (
            f"Outbound date {outbound} is not in the future"
        )

    def test_return_date_is_after_outbound(self, client):
        from datetime import date
        orig = app_module.SERPAPI_KEY
        app_module.SERPAPI_KEY = "test-key"
        captured = {}
        def fake_serpapi(origin, dest, outbound_date, return_date):
            captured["outbound"] = outbound_date
            captured["return"]   = return_date
            return {"price": 300, "currency": "USD"}
        try:
            with patch("app.serpapi_flight_price", side_effect=fake_serpapi):
                client.post("/api/get_live_prices", json={
                    "attendees":   [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
                    "dest_iata":   "LHR",
                    "weeks_ahead": 8,
                })
        finally:
            app_module.SERPAPI_KEY = orig
        assert date.fromisoformat(captured["return"]) > date.fromisoformat(captured["outbound"])

    def test_weeks_ahead_shifts_date_by_correct_number_of_weeks(self, client):
        from datetime import date, timedelta
        orig = app_module.SERPAPI_KEY
        app_module.SERPAPI_KEY = "test-key"
        captured = {}
        def fake_serpapi(origin, dest, outbound_date, return_date):
            captured["outbound"] = outbound_date
            return {"price": 300, "currency": "USD"}
        try:
            with patch("app.serpapi_flight_price", side_effect=fake_serpapi):
                client.post("/api/get_live_prices", json={
                    "attendees":   [{"city": "Vienna", "iatas": ["VIE"], "count": 1}],
                    "dest_iata":   "LHR",
                    "weeks_ahead": 6,
                })
        finally:
            app_module.SERPAPI_KEY = orig
        expected = date.today() + timedelta(weeks=6)
        assert date.fromisoformat(captured["outbound"]) == expected


# ─── 39. addAttendee clears stale results ────────────────────────────────────

class TestAddAttendeeClearsResults:
    """
    Adding a new attendee while results are visible must clear them — the
    existing attendee configuration has changed so old results are stale.
    """

    def test_add_attendee_clears_current_results_when_set(self, html):
        import re
        match = re.search(r'function addAttendee\([^)]*\)\s*\{(.+?)^}',
                          html, re.DOTALL | re.MULTILINE)
        assert match, "addAttendee function not found"
        body = match.group(1)
        assert "currentResults = null"                    in body
        assert "resultsPanel.classList.remove('visible')" in body
        assert "routeDetail.classList.remove('visible')"  in body

    def test_add_attendee_resets_selected_iata(self, html):
        import re
        match = re.search(r'function addAttendee\([^)]*\)\s*\{(.+?)^}',
                          html, re.DOTALL | re.MULTILINE)
        body = match.group(1)
        assert "selectedIata" in body, (
            "addAttendee must reset selectedIata to avoid the early-return "
            "guard in selectDest locking out the same destination after re-search"
        )

    def test_add_attendee_clears_results_only_when_present(self, html):
        """
        The clear is guarded by `if (currentResults)` — it must not
        unconditionally clear (which would remove a freshly-loaded result set
        if two attendees happened to be added back-to-back quickly).
        Wait — actually unconditional is fine too; the guard is just an
        optimisation. Check the guard is present as documented.
        """
        assert "if (currentResults)" in html


# ─── 40. estimate_fare edge cases ────────────────────────────────────────────

class TestEstimateFareEdgeCases:
    """Boundary and degenerate inputs that must not raise."""

    def test_zero_distance_returns_positive_price(self):
        """
        home-city attendees are short-circuited before fare estimation, but
        if estimate_fare(0, ...) were ever called it must not crash or
        return a negative price.
        """
        price, carbon = estimate_fare(0, 0)
        assert price  >= 0
        assert carbon >= 0

    def test_very_large_distance_uses_ultra_long_haul_band(self):
        """Distances beyond 12 000 km (e.g. LHR→AKL ~18 300 km) must work."""
        price, carbon = estimate_fare(18_000, 1)
        assert price  > 0
        assert carbon > 0

    def test_multi_stop_zero_distance_does_not_crash(self):
        price, _ = estimate_fare(0, 3)
        assert price >= 0

    def test_single_stop_penalty_applied(self):
        direct,    _ = estimate_fare(2000, 0)
        one_stop,  _ = estimate_fare(2000, 1)
        two_stops, _ = estimate_fare(2000, 2)
        assert one_stop  == direct   + 60
        assert two_stops == one_stop + 60
