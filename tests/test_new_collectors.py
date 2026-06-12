"""Tests for the Dice and The Muse collectors (network-free via httpx.MockTransport)."""

from __future__ import annotations

import json
from unittest import TestCase

import httpx

from job_market_intel.collectors.dice import DiceCollector
from job_market_intel.collectors.themuse import TheMuseCollector

# ---------------------------------------------------------------------------
# Dice fixtures
# ---------------------------------------------------------------------------

_DICE_GUID_1 = "dc93a174-c7e6-4822-9d6c-0a2940090143"
_DICE_GUID_2 = "ab12cd34-0000-1111-2222-333344445555"


def _dice_search_html(guids: list[str]) -> str:
    cards = "".join(
        f'<div data-testid="job-card"><a data-testid="job-search-job-card-link" '
        f'href="https://www.dice.com/job-detail/{guid}">View</a></div>'
        for guid in guids
    )
    return f"<html><body><div data-testid='job-search-results-container'>{cards}</div></body></html>"


def _dice_detail_html(title: str, company: str, city: str = "Washington", region: str = "DC") -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": "<strong>Duties:</strong><br />Triage SIEM alerts. 1+ years of experience required.",
        "datePosted": "2026-06-09",
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": city,
                "addressRegion": region,
                "addressCountry": "US",
            },
        },
    }
    blob = json.dumps(posting)
    return (
        "<html><body><main>page</main><section>"
        f'<script type="application/ld+json" data-testid="jobDetailStructuredData" '
        f'id="jobDetailStructuredData">{blob}</script>'
        "</section></body></html>"
    )


def _dice_transport(detail_pages: dict[str, str], search_html: str) -> httpx.MockTransport:
    """Route /jobs to the search fixture and /job-detail/{guid} to detail fixtures.
    Search returns the full card list on page 1 and an empty page afterward."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/jobs":
            page = request.url.params.get("page", "1")
            if page == "1":
                return httpx.Response(200, text=search_html)
            return httpx.Response(200, text=_dice_search_html([]))
        for guid, html in detail_pages.items():
            if path == f"/job-detail/{guid}":
                return httpx.Response(200, text=html)
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


class DiceCollectorTests(TestCase):
    def test_collects_listings_from_structured_data(self) -> None:
        transport = _dice_transport(
            search_html=_dice_search_html([_DICE_GUID_1, _DICE_GUID_2]),
            detail_pages={
                _DICE_GUID_1: _dice_detail_html("Help Desk Specialist II", "SAIC"),
                _DICE_GUID_2: _dice_detail_html("SOC Analyst", "Leidos", city="Columbia", region="MD"),
            },
        )
        collector = DiceCollector(throttle_seconds=0, transport=transport)
        result = collector.collect(
            queries=["help desk"], location="United States", results_per_query=0, freshness_days=14
        )

        self.assertEqual(len(result.listings), 2)
        first = result.listings[0]
        self.assertEqual(first.title, "Help Desk Specialist II")
        self.assertEqual(first.company, "SAIC")
        self.assertEqual(first.location, "Washington, DC, US")
        self.assertEqual(first.sources, ["dice"])
        self.assertEqual(first.posted_at, "2026-06-09")
        # HTML stripped from description
        self.assertNotIn("<strong>", first.description)
        self.assertIn("Triage SIEM alerts", first.description)
        self.assertEqual(first.role_bucket, "help_desk_it_admin")
        self.assertEqual(result.listings[1].role_bucket, "junior_soc")

    def test_dedups_detail_urls_across_queries(self) -> None:
        """The same job appearing in two query result sets is fetched once."""
        transport = _dice_transport(
            search_html=_dice_search_html([_DICE_GUID_1]),
            detail_pages={_DICE_GUID_1: _dice_detail_html("IT Support Technician", "Acme")},
        )
        collector = DiceCollector(throttle_seconds=0, transport=transport)
        result = collector.collect(
            queries=["help desk", "IT support"], location="United States", results_per_query=0, freshness_days=14
        )
        self.assertEqual(len(result.listings), 1)

    def test_respects_results_per_query_cap(self) -> None:
        transport = _dice_transport(
            search_html=_dice_search_html([_DICE_GUID_1, _DICE_GUID_2]),
            detail_pages={
                _DICE_GUID_1: _dice_detail_html("Help Desk Analyst", "A"),
                _DICE_GUID_2: _dice_detail_html("Service Desk Analyst", "B"),
            },
        )
        collector = DiceCollector(throttle_seconds=0, transport=transport)
        result = collector.collect(
            queries=["help desk"], location="United States", results_per_query=1, freshness_days=14
        )
        self.assertEqual(len(result.listings), 1)

    def test_missing_structured_data_warns_and_skips(self) -> None:
        transport = _dice_transport(
            search_html=_dice_search_html([_DICE_GUID_1]),
            detail_pages={_DICE_GUID_1: "<html><body>no json here</body></html>"},
        )
        collector = DiceCollector(throttle_seconds=0, transport=transport)
        result = collector.collect(
            queries=["help desk"], location="United States", results_per_query=0, freshness_days=14
        )
        self.assertEqual(len(result.listings), 0)
        self.assertTrue(any("no structured data" in w for w in result.warnings))

    def test_search_failure_warns_not_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error")

        collector = DiceCollector(throttle_seconds=0, transport=httpx.MockTransport(handler))
        result = collector.collect(
            queries=["help desk"], location="United States", results_per_query=0, freshness_days=14
        )
        self.assertEqual(len(result.listings), 0)
        self.assertTrue(any("search page" in w for w in result.warnings))

    def test_remote_location_uses_workplace_type_param(self) -> None:
        seen_params: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/jobs":
                seen_params.append(dict(request.url.params))
                return httpx.Response(200, text=_dice_search_html([]))
            return httpx.Response(404)

        collector = DiceCollector(throttle_seconds=0, transport=httpx.MockTransport(handler))
        collector.collect(queries=["soc analyst"], location="Remote", results_per_query=0, freshness_days=14)
        self.assertTrue(seen_params)
        self.assertEqual(seen_params[0].get("workplaceTypes"), "Remote")
        self.assertNotIn("location", seen_params[0])


# ---------------------------------------------------------------------------
# The Muse fixtures
# ---------------------------------------------------------------------------


def _muse_job(
    name: str,
    company: str,
    locations: list[str],
    *,
    pub_date: str = "2026-06-10T00:00:00Z",
    levels: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "contents": "<p>Provide <b>technical support</b> to end users.</p>",
        "publication_date": pub_date,
        "locations": [{"name": n} for n in locations],
        "levels": [{"name": lv} for lv in (levels or ["Entry Level"])],
        "refs": {"landing_page": "https://www.themuse.com/jobs/x/y"},
        "company": {"name": company},
    }


def _muse_transport(pages: list[list[dict]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        results = pages[page - 1] if page <= len(pages) else []
        return httpx.Response(
            200,
            json={"page": page, "page_count": len(pages), "total": sum(len(p) for p in pages), "results": results},
        )

    return httpx.MockTransport(handler)


class TheMuseCollectorTests(TestCase):
    def test_collects_us_listings_and_strips_html(self) -> None:
        transport = _muse_transport([[_muse_job("IT Help Desk Technician", "Acme", ["New York, NY"])]])
        collector = TheMuseCollector(transport=transport)
        result = collector.collect(queries=[], location="United States", results_per_query=0, freshness_days=14)

        self.assertEqual(len(result.listings), 1)
        li = result.listings[0]
        self.assertEqual(li.title, "IT Help Desk Technician")
        self.assertEqual(li.company, "Acme")
        self.assertEqual(li.location, "New York, NY")
        self.assertNotIn("<p>", li.description)
        self.assertIn("technical support", li.description)
        # Levels metadata appended for the seniority classifier
        self.assertIn("Level: Entry Level", li.description)
        self.assertEqual(li.sources, ["themuse"])

    def test_drops_non_us_listings(self) -> None:
        transport = _muse_transport([[_muse_job("IT Intern", "Bechtel", ["Warsaw, Poland"])]])
        collector = TheMuseCollector(transport=transport)
        result = collector.collect(queries=[], location="United States", results_per_query=0, freshness_days=14)
        self.assertEqual(len(result.listings), 0)

    def test_drops_ambiguous_foreign_city_without_us_state(self) -> None:
        """Live-smoke regression: 'Kazanlak, Bulgaria' leaked through the shared
        is_us_location helper (ambiguous defaults to include). The Muse needs
        positive US evidence — a state-code suffix or 'United States'."""
        transport = _muse_transport(
            [
                [
                    _muse_job("Senior Cloud FinOps Analyst", "Exadel", ["Kazanlak, Bulgaria"]),
                    _muse_job("Help Desk Tech", "Acme", ["Buffalo, NY"]),
                ]
            ]
        )
        collector = TheMuseCollector(transport=transport)
        result = collector.collect(queries=[], location="United States", results_per_query=0, freshness_days=14)
        self.assertEqual(len(result.listings), 1)
        self.assertEqual(result.listings[0].title, "Help Desk Tech")

    def test_flexible_remote_becomes_remote_location(self) -> None:
        transport = _muse_transport([[_muse_job("Desktop Support", "Acme", ["Flexible / Remote"])]])
        collector = TheMuseCollector(transport=transport)
        result = collector.collect(queries=[], location="United States", results_per_query=0, freshness_days=14)
        self.assertEqual(len(result.listings), 1)
        self.assertEqual(result.listings[0].location, "Remote")

    def test_remote_pass_keeps_only_remote(self) -> None:
        transport = _muse_transport(
            [
                [
                    _muse_job("Desktop Support", "Acme", ["Flexible / Remote"]),
                    _muse_job("Help Desk Tech", "Beta", ["Austin, TX"]),
                ]
            ]
        )
        collector = TheMuseCollector(transport=transport)
        result = collector.collect(queries=[], location="Remote", results_per_query=0, freshness_days=14)
        self.assertEqual(len(result.listings), 1)
        self.assertEqual(result.listings[0].title, "Desktop Support")

    def test_stale_listings_dropped_by_freshness(self) -> None:
        transport = _muse_transport([[_muse_job("Old Job", "Acme", ["Austin, TX"], pub_date="2020-01-01T00:00:00Z")]])
        collector = TheMuseCollector(transport=transport)
        result = collector.collect(queries=[], location="United States", results_per_query=0, freshness_days=14)
        self.assertEqual(len(result.listings), 0)

    def test_paginates_until_page_count(self) -> None:
        transport = _muse_transport(
            [
                [_muse_job("Job A", "Acme", ["Austin, TX"])],
                [_muse_job("Job B", "Beta", ["Boston, MA"])],
            ]
        )
        collector = TheMuseCollector(transport=transport)
        result = collector.collect(queries=[], location="United States", results_per_query=0, freshness_days=14)
        self.assertEqual(len(result.listings), 2)

    def test_api_key_passed_when_present(self) -> None:
        seen_params: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json={"page": 1, "page_count": 1, "total": 0, "results": []})

        collector = TheMuseCollector(api_key="muse-key-123", transport=httpx.MockTransport(handler))
        collector.collect(queries=[], location="United States", results_per_query=0, freshness_days=14)
        self.assertEqual(seen_params[0].get("api_key"), "muse-key-123")

    def test_keyless_omits_api_key_param(self) -> None:
        seen_params: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json={"page": 1, "page_count": 1, "total": 0, "results": []})

        collector = TheMuseCollector(transport=httpx.MockTransport(handler))
        collector.collect(queries=[], location="United States", results_per_query=0, freshness_days=14)
        self.assertNotIn("api_key", seen_params[0])

    def test_http_failure_warns_not_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        collector = TheMuseCollector(transport=httpx.MockTransport(handler))
        result = collector.collect(queries=[], location="United States", results_per_query=0, freshness_days=14)
        self.assertEqual(len(result.listings), 0)
        self.assertTrue(any("themuse page 1 failed" in w for w in result.warnings))
