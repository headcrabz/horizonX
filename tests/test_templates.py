"""Tests for GE-02: goal node template library."""
from __future__ import annotations

import pytest

from horizonx.templates.loader import TemplateLibrary


@pytest.fixture()
def lib() -> TemplateLibrary:
    return TemplateLibrary.load()


# ---------------------------------------------------------------------------
# Basic loading
# ---------------------------------------------------------------------------

def test_library_loads(lib):
    templates = lib.all()
    assert len(templates) == 15


def test_all_templates_have_required_fields(lib):
    for t in lib.all():
        assert t.get("id"), f"missing id in {t}"
        assert t.get("name"), f"missing name in {t}"
        assert t.get("description"), f"missing description in {t}"
        assert t.get("domain"), f"missing domain in {t}"


def test_all_templates_have_verification_criteria(lib):
    for t in lib.all():
        criteria = t.get("verification_criteria", [])
        assert isinstance(criteria, list), f"{t['id']}: criteria must be a list"
        assert len(criteria) >= 1, f"{t['id']}: must have at least one verification criterion"


def test_all_criteria_are_strings(lib):
    for t in lib.all():
        for c in t.get("verification_criteria", []):
            assert isinstance(c, str) and len(c) > 10, \
                f"{t['id']}: criterion too short or not a string: {c!r}"


# ---------------------------------------------------------------------------
# Domain coverage
# ---------------------------------------------------------------------------

def test_five_domains_present(lib):
    domains = set(t.get("domain") for t in lib.all())
    assert domains == {"auth", "api", "testing", "refactor", "infra"}


def test_three_templates_per_domain(lib):
    from collections import Counter
    counts = Counter(t.get("domain") for t in lib.all())
    for domain, count in counts.items():
        assert count == 3, f"domain {domain!r} has {count} templates, expected 3"


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def test_get_existing_template(lib):
    t = lib.get("auth.jwt-endpoint")
    assert t is not None
    assert t["id"] == "auth.jwt-endpoint"


def test_get_missing_template_returns_none(lib):
    assert lib.get("nonexistent.template") is None


def test_by_domain(lib):
    auth = lib.by_domain("auth")
    assert len(auth) == 3
    assert all(t["domain"] == "auth" for t in auth)


def test_domains_returns_all(lib):
    domains = lib.domains()
    assert set(domains) == {"auth", "api", "testing", "refactor", "infra"}


# ---------------------------------------------------------------------------
# Template content quality
# ---------------------------------------------------------------------------

def test_descriptions_are_long_enough(lib):
    for t in lib.all():
        desc = str(t.get("description", "")).strip()
        assert len(desc) >= 50, f"{t['id']}: description too short ({len(desc)} chars)"


def test_suggested_validators_present(lib):
    for t in lib.all():
        validators = t.get("suggested_validators", [])
        assert len(validators) >= 1, f"{t['id']}: no suggested validators"


def test_validator_configs_have_type_and_command(lib):
    for t in lib.all():
        for v in t.get("suggested_validators", []):
            assert "type" in v, f"{t['id']}: validator missing 'type'"
            cmd = v.get("config", {}).get("command")
            assert cmd, f"{t['id']}: validator missing config.command"


# ---------------------------------------------------------------------------
# API endpoint (requires dashboard extras — skipped in base CI)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_returns_15_templates():
    pytest.importorskip("fastapi", reason="pip install horizonx[dashboard]")
    from httpx import ASGITransport, AsyncClient

    from horizonx.dashboard.app import create_app
    app = create_app(db_path=":memory:")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/templates/goal-nodes")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 15


@pytest.mark.asyncio
async def test_api_get_single_template():
    pytest.importorskip("fastapi", reason="pip install horizonx[dashboard]")
    from httpx import ASGITransport, AsyncClient

    from horizonx.dashboard.app import create_app
    app = create_app(db_path=":memory:")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/templates/goal-nodes/auth.jwt-endpoint")
    assert r.status_code == 200
    assert r.json()["id"] == "auth.jwt-endpoint"


@pytest.mark.asyncio
async def test_api_404_for_missing_template():
    pytest.importorskip("fastapi", reason="pip install horizonx[dashboard]")
    from httpx import ASGITransport, AsyncClient

    from horizonx.dashboard.app import create_app
    app = create_app(db_path=":memory:")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/templates/goal-nodes/nope.nope")
    assert r.status_code == 404
