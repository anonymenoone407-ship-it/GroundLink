import ast
from pathlib import Path

import pandas as pd


DASHBOARD = Path(__file__).with_name("dashboard.py")


def load_pure_helpers():
    tree = ast.parse(DASHBOARD.read_text(encoding="utf-8"))
    wanted = {"latest_reception_events", "display_value", "abbreviated_run_id"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"pd": pd}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(DASHBOARD), "exec"), namespace)
    return namespace


def test_latest_reception_is_descending_limited_and_filter_independent():
    helper = load_pure_helpers()["latest_reception_events"]
    events = pd.DataFrame({
        "id": range(1, 16), "scenario": ["ideal"] * 7 + ["replay"] * 8,
        "run_id": ["run-a"] * 12 + ["run-b"] * 3,
        "status": ["ACCEPT", "REJECT"] * 7 + ["ACCEPT"],
    })
    latest = helper(events)
    assert latest["id"].tolist() == list(range(15, 5, -1))
    assert len(latest) == 10
    assert set(latest["scenario"]) == {"ideal", "replay"}
    assert set(latest["run_id"]) == {"run-a", "run-b"}
    assert int((latest["status"] == "ACCEPT").sum()) == 5
    assert int((latest["status"] == "REJECT").sum()) == 5


def test_latest_reception_handles_less_than_ten_and_empty():
    helper = load_pure_helpers()["latest_reception_events"]
    small = pd.DataFrame({"id": [2, 1], "status": ["REJECT", "ACCEPT"]})
    assert helper(small)["id"].tolist() == [2, 1]
    assert helper(small.iloc[0:0]).empty


def test_live_section_placement_columns_and_security_guards():
    source = DASHBOARD.read_text(encoding="utf-8")
    assert source.index('st.title("🛰️ GroundLink Guardian")') < source.index(
        "render_live_reception(df)"
    ) < source.index('st.subheader("Secure Satellite Ocean-Wind Downlink")')
    for label in (
        "Event ID", "Reception Time", "Scenario", "Run ID", "Satellite ID",
        "Sequence Number", "EVM (%)", "RF Status", "Security Decision", "Decision Reason",
    ):
        assert label in source
    assert 'trusted = status == "ACCEPT"' in source
    assert 'events.sort_values("id", ascending=False).head(limit)' in source
    assert "export_data = latest[export_columns].copy()" in source
    assert 'export_data["status"] != "ACCEPT"' in source
    assert source.index("render_live_reception(df)") < source.index(
        "df = add_historical_diagnostics(df)"
    )


def test_query_contains_authenticated_identity_fields():
    source = DASHBOARD.read_text(encoding="utf-8")
    select = source[source.index("SELECT"):source.index("FROM rx_events")]
    assert "satellite_id" in select
    assert "sequence_number" in select
