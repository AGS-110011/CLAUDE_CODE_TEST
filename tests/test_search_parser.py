"""Tests for search pagination helpers — no network required."""
from __future__ import annotations

from pathlib import Path

from idealista_extractor.scraper.search import (
    _build_page_url,
    _extract_listing_urls,
    _has_next_page,
    _parse_total_count,
)

FIXTURES = Path(__file__).parent / "fixtures"

BASE_URL = (
    "https://www.idealista.com/areas/venta-viviendas/con-pisos,apartamentos/"
    "?shape=%28%28test%29%29"
)


class TestBuildPageUrl:
    def test_page_1_adds_sort(self):
        url = _build_page_url(BASE_URL, 1)
        assert "ordenado-por=fecha-publicacion-desc" in url
        assert "pagina-" not in url

    def test_page_2_inserts_pagina(self):
        url = _build_page_url(BASE_URL, 2)
        assert "/pagina-2.htm" in url
        assert "shape=" in url
        assert "ordenado-por=fecha-publicacion-desc" in url

    def test_page_5(self):
        url = _build_page_url(BASE_URL, 5)
        assert "/pagina-5.htm" in url

    def test_shape_preserved(self):
        url = _build_page_url(BASE_URL, 3)
        assert "shape=%28%28test%29%29" in url

    def test_sort_not_duplicated(self):
        url_with_sort = BASE_URL + "&ordenado-por=fecha-publicacion-desc"
        url = _build_page_url(url_with_sort, 1)
        assert url.count("ordenado-por") == 1

    def test_page_2_sort_after_shape(self):
        url = _build_page_url(BASE_URL, 2)
        q = url.split("?", 1)[1]
        assert "shape=" in q
        assert "ordenado-por=" in q


class TestParseTotalCount:
    def test_standard(self):
        assert _parse_total_count("143 anuncios de pisos") == 143

    def test_with_dot_thousands(self):
        assert _parse_total_count("1.234 anuncios") == 1234

    def test_singular(self):
        assert _parse_total_count("1 anuncio disponible") == 1

    def test_none_when_absent(self):
        assert _parse_total_count("<html>no count here</html>") is None


class TestExtractListingUrls:
    def test_from_fixture(self):
        html = (FIXTURES / "search_page_sample.html").read_text(encoding="utf-8")
        urls = _extract_listing_urls(html)
        assert len(urls) == 3
        assert all("/inmueble/" in u for u in urls)

    def test_dedup_not_done_here(self):
        html = '<a href="/inmueble/111/"></a><a href="/inmueble/111/"></a>'
        urls = _extract_listing_urls(html)
        assert len(urls) == 2  # dedup happens in paginate_search

    def test_empty_page(self):
        assert _extract_listing_urls("<html><body>no items</body></html>") == []


class TestHasNextPage:
    def test_next_present(self):
        html = (FIXTURES / "search_page_sample.html").read_text(encoding="utf-8")
        assert _has_next_page(html) is True

    def test_no_next(self):
        html = '<ul class="pagination"><li class="prev"><a>Ant</a></li></ul>'
        assert _has_next_page(html) is False

    def test_disabled_next(self):
        html = '<li class="next disabled"><a>Sig</a></li>'
        assert _has_next_page(html) is False
