"""Continuous Monitoring's diff engine (Build Plan Component 9): compares a new scan's
evidence against the approved baseline and reports added/removed/changed items per
evidence type. A pure function over two evidence dicts (the same shape
scan_repository.get_scan_evidence_summary_concurrent() returns) — no DB access here,
so it's directly unit-testable without a live database.
"""

from typing import Any

# Which evidence-type key doubles as its natural identity for diffing. Trackers key on
# the query-stripped path (matching tracker_detector.py's own dedup key) so a
# cache-busted param doesn't look like a removed+added pair of distinct trackers.
_KEY_FNS = {
    "pages": lambda item: item["url"],
    "cookies": lambda item: (item["name"], item.get("domain")),
    "trackers": lambda item: item["script_src"].split("?", 1)[0],
    "third_party_services": lambda item: item["service_name"],
    "policies": lambda item: (item["url"], item.get("policy_type")),
}

# Fields worth reporting as a "changed" entry when the identity matches but the
# category/vendor/status differs — a re-classified item is real, useful signal for a
# reviewer even though nothing was added or removed.
_COMPARE_FIELDS = {
    "cookies": ("category", "vendor", "is_first_party"),
    "trackers": ("category", "vendor"),
    "third_party_services": ("category",),
    "pages": ("http_status",),
}

_NON_ESSENTIAL_CATEGORIES = {"analytics", "marketing"}


def _index(items: list[dict], key_fn) -> dict[Any, dict]:
    return {key_fn(item): item for item in items}


def diff_evidence(baseline: dict, new: dict) -> dict:
    """Returns {"added": {...}, "removed": {...}, "changed": {...}, "has_material_change": bool},
    each of added/removed/changed keyed by evidence type (only non-empty types included)."""
    added: dict[str, list[dict]] = {}
    removed: dict[str, list[dict]] = {}
    changed: dict[str, list[dict]] = {}
    material = False

    for evidence_type, key_fn in _KEY_FNS.items():
        base_items = _index(baseline.get(evidence_type, []), key_fn)
        new_items = _index(new.get(evidence_type, []), key_fn)

        added_keys = new_items.keys() - base_items.keys()
        removed_keys = base_items.keys() - new_items.keys()
        common_keys = base_items.keys() & new_items.keys()

        if added_keys:
            added[evidence_type] = [new_items[k] for k in added_keys]
        if removed_keys:
            removed[evidence_type] = [base_items[k] for k in removed_keys]

        compare_fields = _COMPARE_FIELDS.get(evidence_type, ())
        type_changes = []
        for k in common_keys:
            old_item, new_item = base_items[k], new_items[k]
            diffs = {
                f: {"from": old_item.get(f), "to": new_item.get(f)}
                for f in compare_fields
                if old_item.get(f) != new_item.get(f)
            }
            if diffs:
                type_changes.append({"key": k if isinstance(k, str) else list(k), "fields": diffs})
        if type_changes:
            changed[evidence_type] = type_changes

        # Material: any cookie/tracker added or removed, or any re-classification into/
        # out of a non-essential (analytics/marketing) category. A new/removed page or
        # a re-classified third-party service is real signal but not on its own
        # "material" in the DPDP-risk sense this flag exists to gate on.
        if evidence_type in ("cookies", "trackers"):
            if added_keys or removed_keys:
                material = True
            for change in type_changes:
                cat = change["fields"].get("category", {})
                if cat and (cat.get("from") in _NON_ESSENTIAL_CATEGORIES) != (cat.get("to") in _NON_ESSENTIAL_CATEGORIES):
                    material = True

    baseline_signal = (baseline.get("consent_signals") or [{}])[0]
    new_signal = (new.get("consent_signals") or [{}])[0]
    signal_fields = ("mechanism_type", "has_reject_all", "has_granular_choices")
    signal_diffs = {
        f: {"from": baseline_signal.get(f), "to": new_signal.get(f)}
        for f in signal_fields
        if baseline_signal.get(f) != new_signal.get(f)
    }
    if signal_diffs:
        changed["consent_signals"] = [{"fields": signal_diffs}]
        material = True  # a changed consent mechanism is always material

    return {"added": added, "removed": removed, "changed": changed, "has_material_change": material}
