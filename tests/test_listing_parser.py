"""Parser tests against saved HTML fixtures — no network required."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from idealista_extractor.scraper.listing import (
    _clean_price,
    _clean_size,
    _clean_year,
    _extract_from_json,
    _map_condition,
    _parse_date,
    _parse_features,
    _scrape_address_from_html,
    _scrape_feature_bullets,
    _scrape_features_from_html,
    _scrape_price_from_html,
    _source_id_from_url,
    _yes_no,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Unit tests for individual normaliser functions
# ---------------------------------------------------------------------------

class TestCleanPrice:
    def test_plain_int(self):
        assert _clean_price("285000") == 285_000

    def test_with_dots(self):
        assert _clean_price("285.000") == 285_000

    def test_with_euro(self):
        assert _clean_price("285.000 €") == 285_000

    def test_none(self):
        assert _clean_price(None) is None

    def test_int_input(self):
        assert _clean_price(1450) == 1450


class TestCleanSize:
    def test_plain(self):
        assert _clean_size("85") == 85.0

    def test_with_unit(self):
        assert _clean_size("85 m²") == 85.0

    def test_decimal_comma(self):
        assert _clean_size("85,5") == 85.5

    def test_none(self):
        assert _clean_size(None) is None


class TestCleanYear:
    def test_valid(self):
        assert _clean_year("1975") == 1975

    def test_too_old(self):
        assert _clean_year("1200") is None

    def test_future(self):
        assert _clean_year("2050") is None

    def test_none(self):
        assert _clean_year(None) is None


class TestMapCondition:
    def test_new(self):
        assert _map_condition("obra nueva") == "New"
        assert _map_condition("A estrenar") == "New"

    def test_renovated(self):
        assert _map_condition("buen estado") == "Renovated"
        assert _map_condition("reformado") == "Renovated"
        assert _map_condition("segunda mano/buen estado") == "Renovated"

    def test_to_renovate(self):
        assert _map_condition("a reformar") == "To renovate"
        assert _map_condition("necesita reforma") == "To renovate"

    def test_unknown(self):
        assert _map_condition("desconocido") is None

    def test_none(self):
        assert _map_condition(None) is None


class TestParseDate:
    def test_iso(self):
        assert _parse_date("2026-04-21") == date(2026, 4, 21)

    def test_hace_dias(self):
        from datetime import timedelta
        result = _parse_date("Publicado hace 5 días")
        assert result == date.today() - timedelta(days=5)

    def test_hoy(self):
        assert _parse_date("hoy") == date.today()

    def test_none(self):
        assert _parse_date(None) is None


class TestYesNo:
    def test_bool_true(self):
        assert _yes_no(True) == "Yes"

    def test_bool_false(self):
        assert _yes_no(False) == "No"

    def test_string_true(self):
        assert _yes_no("true") == "Yes"

    def test_string_false(self):
        assert _yes_no("false") == "No"

    def test_none(self):
        assert _yes_no(None) is None


class TestSourceIdFromUrl:
    def test_standard(self):
        assert _source_id_from_url("https://www.idealista.com/inmueble/12345678/") == "12345678"

    def test_no_trailing_slash(self):
        assert _source_id_from_url("https://www.idealista.com/inmueble/99999999") == "99999999"


# ---------------------------------------------------------------------------
# Integration tests against HTML fixtures
# ---------------------------------------------------------------------------

class TestSaleFixture:
    @pytest.fixture(autouse=True)
    def load_html(self):
        self.html = (FIXTURES / "sale_sample.html").read_text(encoding="utf-8")

    def test_json_extraction(self):
        data = _extract_from_json(self.html)
        assert data.get("price") == "285000"
        assert data.get("surface") == "85"
        assert data.get("rooms") == "3"
        assert data.get("bathrooms") == "2"

    def test_price_fallback(self):
        price = _scrape_price_from_html(self.html)
        assert price == 285_000

    def test_features_fallback(self):
        fb = _scrape_features_from_html(self.html)
        assert fb["size"] == 85.0
        assert fb["rooms"] == 3
        assert fb["bathrooms"] == 2

    def test_address(self):
        addr = _scrape_address_from_html(self.html)
        assert "Mayor" in addr or "Madrid" in addr

    def test_feature_bullets(self):
        bullets = _scrape_feature_bullets(self.html)
        joined = " ".join(b.lower() for b in bullets)
        assert "ascensor" in joined or "terraza" in joined or "garaje" in joined

    def test_feature_parsing_elevator(self):
        bullets = _scrape_feature_bullets(self.html)
        notes: list[str] = []
        result = _parse_features(bullets, notes)
        assert result.get("elevator") == "Yes"

    def test_feature_parsing_terrace(self):
        bullets = _scrape_feature_bullets(self.html)
        notes: list[str] = []
        result = _parse_features(bullets, notes)
        assert result.get("terrace") == "Yes"

    def test_feature_parsing_parking(self):
        bullets = _scrape_feature_bullets(self.html)
        notes: list[str] = []
        result = _parse_features(bullets, notes)
        assert result.get("parking") == "Yes"

    def test_condition_from_json(self):
        data = _extract_from_json(self.html)
        cond = _map_condition(data.get("conservationState"))
        assert cond == "Renovated"


class TestRentFixture:
    @pytest.fixture(autouse=True)
    def load_html(self):
        self.html = (FIXTURES / "rent_sample.html").read_text(encoding="utf-8")

    def test_json_extraction(self):
        data = _extract_from_json(self.html)
        assert data.get("price") == "1450"
        assert data.get("surface") == "70"

    def test_price_fallback(self):
        price = _scrape_price_from_html(self.html)
        assert price == 1450

    def test_terrace_balcony(self):
        bullets = _scrape_feature_bullets(self.html)
        notes: list[str] = []
        result = _parse_features(bullets, notes)
        # balcón → terrace=No, note "balcony only"
        assert result.get("terrace") == "No"
        assert "balcony only" in notes

    def test_optional_parking_note(self):
        bullets = _scrape_feature_bullets(self.html)
        notes: list[str] = []
        _parse_features(bullets, notes)
        assert any("optional parking" in n for n in notes)

    def test_condition_renovated(self):
        data = _extract_from_json(self.html)
        cond = _map_condition(data.get("conservationState"))
        assert cond == "Renovated"

    def test_listing_date_from_json(self):
        data = _extract_from_json(self.html)
        d = _parse_date(data.get("publishDate"))
        assert d == date(2026, 4, 24)
