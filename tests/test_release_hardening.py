"""Regressions for public-release boundaries; all facts are synthetic."""

from copy import deepcopy
from datetime import date
import json

import polars as pl
import pytest

from contract_coverage import analyze as original_analyze, export_csv, synthetic_example
from product_coverage import analyze, evaluate
from product_coverage.cli import main
from product_coverage.files import (
    file_sha256,
    fingerprint,
    load_json,
    read_policy,
    resolve_inputs,
    scan_files,
    write_report,
)
from product_coverage.presentation import safe_cell
from tests.test_product_coverage import row


def fitted(method="monthly-calendar"):
    rows = [row("a", 99, pattern="C1"), row("b", 1, delivery_month="2024-03", pattern="C2")]
    return list(analyze(pl.DataFrame(rows).lazy(), method=method))[0]


def later():
    return pl.DataFrame(
        [
            row("a25", 99, date="2025-01-03", delivery_month="2025-02", pattern="C1"),
            row("b25", 1, date="2025-01-03", delivery_month="2025-03", pattern="C2"),
        ]
    ).lazy()


def check(policy):
    return list(evaluate(later(), [policy], start=date(2025, 1, 1), universe_complete=True))


def test_in_memory_policy_cannot_exceed_capacity_even_with_changed_rule_count():
    policy = fitted()
    extra = policy.rules.with_columns(
        pl.lit("Y0-M03").alias("candidate"), pl.lit(2).cast(pl.UInt32).alias("priority")
    )
    policy.rules = pl.concat([policy.rules, extra])
    policy.summary["selected_rules"] = 2
    with pytest.raises(ValueError, match="capacity"):
        check(policy)


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5, "1"])
def test_invalid_capacity_rejected(capacity):
    policy = fitted()
    policy.summary["minimum_capacity"] = capacity
    with pytest.raises(ValueError):
        check(policy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "0"),
        ("state", "13"),
        ("state", "all"),
        ("candidate", "Y0-M13"),
        ("candidate", "Y00-M02"),
        ("candidate", "Y0-M02/Y0-M03"),
        ("candidate", ""),
        ("priority", 0),
        ("priority", 2),
        ("priority", -1),
        ("volume", 0),
        ("volume", -1),
    ],
)
def test_malformed_rules_rejected(field, value):
    policy = fitted()
    policy.rules = policy.rules.with_columns(pl.lit(value).alias(field))
    with pytest.raises(ValueError):
        check(policy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("captured", 1),
        ("known_volume", 1),
        ("capacity_minus_one_captured", 99),
        ("selected_rules", 2),
        ("target", "0"),
        ("target", 0.99),
        ("end", "2023-01-01"),
        ("target_met", False),
        ("optimality", "heuristic"),
    ],
)
def test_inconsistent_certificates_rejected(field, value):
    policy = fitted()
    policy.summary[field] = value
    with pytest.raises(ValueError):
        check(policy)


def test_duplicate_priorities_and_bad_spread_order_rejected():
    policy = list(
        analyze(pl.DataFrame([row("a", 50), row("b", 50, delivery_month="2024-03")]).lazy())
    )[0]
    policy.rules = policy.rules.with_columns(pl.lit(1).alias("priority"))
    with pytest.raises(ValueError, match="priorities"):
        check(policy)
    spread = fitted()
    spread.summary["category"] = "listed_spread"
    spread.rules = spread.rules.with_columns(pl.lit("Y1-M02/Y0-M03").alias("candidate"))
    with pytest.raises(ValueError, match="near/far"):
        check(spread)


def test_json_checksum_and_binding_reject_edits_and_replacements(tmp_path):
    write_report([fitted()], tmp_path / "policy", {})
    policy = tmp_path / "policy"
    rules = policy / "group-000001/rules.json"
    original = rules.read_text()
    rules.write_text(original.replace("Y0-M02", "Y0-M03"))
    with pytest.raises(ValueError, match="checksum"):
        read_policy(policy)
    rules.write_text(original)
    report = load_json(policy / "report.json")
    report["groups"][0]["minimum_capacity"] = 2
    (policy / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="training summary"):
        read_policy(policy)
    rules.unlink()
    with pytest.raises(OSError):
        read_policy(policy)


def test_rehashed_invalid_policy_still_fails_semantic_validation(tmp_path):
    write_report([fitted()], tmp_path / "policy", {})
    path = tmp_path / "policy"
    report = load_json(path / "report.json")
    rule_file = path / "group-000001/rules.json"
    payload = load_json(rule_file)
    payload["rules"].append({**payload["rules"][0], "candidate": "Y0-M03", "priority": 2})
    payload["summary"]["selected_rules"] = 2
    report["groups"][0]["selected_rules"] = 2
    rule_file.write_text(json.dumps(payload))
    report["groups"][0]["rules_sha256"] = file_sha256(rule_file)
    (path / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="capacity"):
        read_policy(path)


@pytest.mark.parametrize(
    "field,value",
    [("volume", 99.9), ("volume", True), ("priority", 1.9), ("state", 1), ("candidate", 1)],
)
def test_json_rule_types_cannot_be_silently_coerced(tmp_path, field, value):
    path = tmp_path / "policy"
    write_report([fitted()], path, {})
    rule_file = path / "group-000001/rules.json"
    payload = load_json(rule_file)
    payload["rules"][0][field] = value
    rule_file.write_text(json.dumps(payload))
    report = load_json(path / "report.json")
    report["groups"][0]["rules_sha256"] = file_sha256(rule_file)
    (path / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        read_policy(path)


def test_presentation_edits_do_not_change_frozen_rules_and_resaving_is_valid(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    write_report([fitted()], first, {})
    csv = first / "group-000001/rules.csv"
    csv.write_text(csv.read_text().replace("Y0-M02", "Y0-M03"))
    loaded = read_policy(first)
    assert loaded[0].rules["candidate"].to_list() == ["Y0-M02"]
    write_report(loaded, second, {})
    assert read_policy(second)[0].rules.equals(loaded[0].rules)


def test_duplicate_json_fields_nonfinite_legacy_and_traversal_rejected(tmp_path):
    path = tmp_path / "bad.json"
    for data in ['{"a":1,"a":2}', '{"a":NaN}', "[]"]:
        path.write_text(data)
        with pytest.raises(ValueError):
            load_json(path)
    policy = tmp_path / "policy"
    write_report([fitted()], policy, {})
    report = load_json(policy / "report.json")
    old = deepcopy(report)
    old["version"] = 1
    (policy / "report.json").write_text(json.dumps(old))
    with pytest.raises(ValueError, match="regenerate legacy"):
        read_policy(policy)
    report["groups"][0]["directory"] = "../outside"
    (policy / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="path"):
        read_policy(policy)


@pytest.mark.parametrize("method", ["monthly-calendar", "fixed-products", "fixed-patterns"])
def test_valid_machine_policy_roundtrip_is_unchanged(tmp_path, method):
    trained = fitted(method)
    write_report([trained], tmp_path / "policy", {})
    loaded = read_policy(tmp_path / "policy")[0]
    assert loaded.rules.select("state", "candidate").equals(
        trained.rules.select("state", "candidate")
    )
    assert check(loaded)[0].summary["captured"] == check(trained)[0].summary["captured"]


@pytest.mark.parametrize(
    "identifier",
    [
        "=1+1",
        "+SUM(A1)",
        "-ID",
        "@x",
        " =1",
        "\t=1",
        "x\ny",
        "x\ry",
        "x\x00y",
        "'x",
        "\ufeff=1",
        "\u200b=1",
        "\uff1d1+1",
    ],
)
def test_safe_spreadsheet_export_preserves_machine_identity(tmp_path, identifier):
    data = pl.DataFrame([row(identifier, 100)])
    result = list(analyze(data.lazy(), method="fixed-products"))[0]
    write_report([result], tmp_path / "policy", {})
    csv = pl.read_csv(tmp_path / "policy/group-000001/rules.csv")
    assert csv["candidate"][0] == safe_cell(identifier)
    assert csv["candidate"][0].startswith("'")
    assert not any(ord(c) < 32 for c in csv["candidate"][0])
    assert read_policy(tmp_path / "policy")[0].rules["candidate"][0] == identifier
    assert safe_cell(-12.5) == -12.5
    assert safe_cell('normal,quoted"id') == 'normal,quoted"id'


def test_legacy_selector_csv_escapes_but_json_does_not_change(tmp_path):
    report = original_analyze(synthetic_example())
    report["recommendations"][0]["instrument_id"] = "=1+1"
    out = tmp_path / "legacy.csv"
    export_csv(report, out)
    assert "'=1+1" in out.read_text()
    assert report["recommendations"][0]["instrument_id"] == "=1+1"


def test_unicode_family_summary_uses_utf8(tmp_path):
    facts = pl.DataFrame([row(family="東京", product_id="限月", volume=100)])
    out = tmp_path / "report"
    write_report(analyze(facts.lazy()), out, {})
    assert "東京" in (out / "summary.md").read_text(encoding="utf-8")
    assert read_policy(out)[0].summary["family"] == "東京"


def test_literal_glob_directory_duplicates_and_spaces(tmp_path):
    nested = tmp_path / "data with spaces/year=2024"
    nested.mkdir(parents=True)
    one = nested / "one[1].parquet"
    pl.DataFrame([row()]).write_parquet(one)
    assert resolve_inputs([tmp_path / "data with spaces", one, nested / "*.parquet"]) == [
        one.resolve()
    ]
    assert scan_files([tmp_path / "data with spaces"]).collect().height == 1
    out = tmp_path / "report"
    assert main(["analyze", "--input", str(nested / "*.parquet"), "--out", str(out)]) == 0
    provenance = load_json(out / "report.json")["provenance"]
    assert "path" not in provenance["inputs"][0]
    assert str(tmp_path) not in json.dumps(provenance)
    assert fingerprint([one], include_paths=True)[0]["path"] == str(one.resolve())
    with pytest.raises(ValueError, match="matched no files"):
        resolve_inputs([nested / "missing*.parquet"])
    with pytest.raises(ValueError, match="At least one"):
        resolve_inputs([])
    unsupported = tmp_path / "bad.txt"
    unsupported.write_text("not facts")
    with pytest.raises(ValueError, match="CSV or Parquet"):
        resolve_inputs([unsupported])
