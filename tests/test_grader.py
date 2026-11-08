import json, pathlib
import pytest
from roadworthy.eval.models import AgentRun, ScenarioSpec, ToolCall
from roadworthy.eval.grader import grade_run
from roadworthy.eval.diff import diff_snapshots
from roadworthy.eval.verifier import verify_claim

FX = json.loads((pathlib.Path(__file__).parent / "fixtures" / "grading_fixtures.json").read_text())
KEYS = ["outcome", "task_success", "false_claims", "unverifiable_claims", "harmful_effects", "duplicate_effects",
        "missing_effects", "attempted_forbidden_tools", "claims_malformed"]


def build(fx):
    r = dict(fx["run"]); r["tool_calls"] = [ToolCall(**c) for c in r["tool_calls"]]
    return AgentRun(**r), ScenarioSpec(**fx["spec"])


def test_fixture_set_is_12_and_synthetic():
    assert len(FX) == 12 and all(f["run"]["synthetic"] for f in FX)


def test_grader_agrees_with_all_hand_labelled_fixtures():
    bad = []
    for fx in FX:
        g = grade_run(*build(fx))
        got = {k: getattr(g, k) for k in KEYS}
        if got != fx["expect"]:
            bad.append((fx["name"], {k: (fx["expect"][k], got[k]) for k in KEYS if got[k] != fx["expect"][k]}))
    assert not bad, f"disagreements: {bad}"          # 100% agreement required


def test_three_replays_have_identical_verdict_hashes():
    hashes = [[grade_run(*build(fx)).verdict_hash for fx in FX] for _ in range(3)]
    assert hashes[0] == hashes[1] == hashes[2]


def test_hash_changes_when_evidence_changes():
    a = grade_run(*build(FX[0])); b = grade_run(*build(FX[1]))
    assert a.verdict_hash != b.verdict_hash


def test_llm_agent_must_name_a_model_and_baseline_must_not():
    base = dict(run_id="r", scenario_id="s", agent_id="a", tool_calls=[], final_message="", before={}, after={})
    with pytest.raises(ValueError): AgentRun(agent_kind="llm_agent", model=None, **base)
    with pytest.raises(ValueError): AgentRun(agent_kind="scripted_baseline", model="gpt-x", **base)
    with pytest.raises(ValueError): AgentRun(agent_kind="human", model=None, **base)


def test_decimal_normalisation_in_state_claims():
    after = {"refunds": {"R": {"amount": "1.0"}}}
    assert verify_claim({"type": "state", "section": "refunds", "id": "R", "field": "amount", "equals": "1.00"}, after, [])[0] == "VERIFIED"


def test_unobservable_sections_never_produce_a_pass():
    before = {"invoices": {"__unavailable__": {}}}; after = {"invoices": {"__unavailable__": {}}}
    assert diff_snapshots(before, after)[0]["kind"] == "unobservable"
    assert verify_claim({"type": "no_change"}, after, diff_snapshots(before, after))[0] == "UNVERIFIABLE"
