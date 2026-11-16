"""The DPDP registry seed, the effective-date rules, and migration 0052."""
import re
from datetime import date
from pathlib import Path

import pytest

from app.enterprise.models import EntRegistryProvision, EntRegistryVersion
from app.enterprise.registry import applicability, loader
from app.scoring import legal_map

SQL = (Path(__file__).resolve().parents[2] / "migrations" / "0052_enterprise_registry.sql").read_text(encoding="utf-8")


def test_the_seed_is_exactly_the_verified_legal_map_so_it_cannot_drift():
    """Every phrase in legal_map is checked against the Act and Rules PDFs by tests/test_legal_map.py; the registry seed must be the same set, field for field."""
    seed = loader.read_seed()
    expected = [
        {"instrument": a.instrument, "ref": a.ref, "summary": a.summary, "relation": a.relation, "phrase": a.phrase, "commences": a.commences.isoformat()}
        for a in legal_map.all_anchors()
    ]
    assert seed["provisions"] == expected


def test_the_seed_says_plainly_that_it_is_not_every_section_and_rule():
    assert "NOT every section" in loader.read_seed()["coverage_note"]


def test_a_bad_seed_is_refused(tmp_path):
    base = loader.read_seed()
    for mutate, message in [
        (lambda p: p.update(instrument="Regulation"), "instrument"),
        (lambda p: p.update(relation="vibes"), "relation"),
        (lambda p: p.update(phrase="  "), "required"),
        (lambda p: p.update(commences="soon"), None),
    ]:
        doc = {**base, "provisions": [dict(base["provisions"][0])]}
        mutate(doc["provisions"][0])
        f = tmp_path / "s.json"
        f.write_text(__import__("json").dumps(doc), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            loader.read_seed(f)
    dup = {**base, "provisions": [base["provisions"][0], base["provisions"][0]]}
    f = tmp_path / "d.json"
    f.write_text(__import__("json").dumps(dup), encoding="utf-8")
    with pytest.raises(loader.RegistryError, match="twice"):
        loader.read_seed(f)


def test_the_version_label_names_the_legal_mapping_version():
    assert loader.version_label(loader.read_seed()).startswith("dpdp-20")


def test_an_approval_needs_a_name():
    import asyncio

    with pytest.raises(loader.RegistryError):
        asyncio.run(loader.approve(None, None, "  "))


# ---- effective dates: the provisions do not all commence together ----
NOV_2025, NOV_2026, MAY_2027 = date(2025, 11, 13), date(2026, 11, 13), date(2027, 5, 13)


def test_a_provision_is_in_force_from_its_commencement_date_and_not_before():
    assert applicability.effectiveness(MAY_2027, date(2027, 5, 13)).in_force
    assert applicability.effectiveness(MAY_2027, date(2027, 5, 12)).status == applicability.NOT_YET_EFFECTIVE
    assert applicability.effectiveness(MAY_2027, date(2027, 5, 12)).days_until == 1
    assert applicability.effectiveness(NOV_2025, date(2026, 11, 12)).in_force


def test_the_same_day_gives_different_answers_for_different_provisions():
    """On 2026-12-01 the November 2026 provisions are in force and the May 2027 ones are not: they must never be treated alike."""
    on = date(2026, 12, 1)
    assert applicability.effectiveness(NOV_2026, on).in_force and not applicability.effectiveness(MAY_2027, on).in_force


def test_splitting_by_effectiveness_drops_nothing():
    anchors = legal_map.all_anchors()
    now, later = applicability.split_by_effectiveness(anchors, date(2026, 12, 1))
    assert len(now) + len(later) == len(anchors) and later


# ---- migration 0052 ----
def _block(table):
    m = re.search(rf"create table if not exists {table} \((.*?)\n\);", SQL, re.S)
    assert m, table
    return m.group(1)


@pytest.mark.parametrize("model", [EntRegistryVersion, EntRegistryProvision])
def test_every_registry_model_column_is_in_the_migration(model):
    block = _block(model.__tablename__)
    for col in model.__table__.columns:
        assert re.search(rf"^\s+{col.name}\s", block, re.M), f"{model.__tablename__}.{col.name}"


def test_an_approved_version_is_frozen_by_the_database_and_is_additive_only():
    assert "ent_registry_provisions_freeze" in SQL and "ent_registry_versions_freeze" in SQL
    assert "ent_registry_approved_has_approver" in SQL
    assert not re.search(r"alter table (?!ent_)\w+", SQL) and not re.search(r"drop table|truncate|delete from", SQL, re.I)
    code = "\n".join(line for line in SQL.splitlines() if not line.strip().startswith("--"))
    assert "org_id" not in code      # reference data shared by every organisation, not tenant data
