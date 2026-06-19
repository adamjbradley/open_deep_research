"""Offline unit test for the NVIDIA role-fit benchmark harness (tests/bench/).

Uses a fake model + fake judge + fake clock -- NO live API calls -- so the runner's
aggregation, the backend-fatal short-circuit, the report rendering, and each StageProbe's
is_valid contract are all covered for free. The live benchmark itself is run on demand via
`python -m tests.bench.nvidia_role_fit` and is never collected by CI.
"""
import asyncio

from langchain_core.messages import AIMessage

from tests.bench import report, runner
from tests.bench.runner import CellResult, RepResult, matrix_from_json, run_cell, run_matrix
from tests.bench.stages import STAGES_BY_NAME


# -- fakes --------------------------------------------------------------------
class _FakeModel:
    """A configurable stand-in for a chat model. ``behavior`` decides each ainvoke."""

    def __init__(self, behavior):
        self._behavior = behavior

    def with_structured_output(self, schema, *a, **k):
        return self

    def bind_tools(self, tools, *a, **k):
        return self

    def with_retry(self, *a, **k):
        return self

    def with_config(self, *a, **k):
        return self

    async def ainvoke(self, *_a, **_k):
        return self._behavior()


def _clock():
    """Deterministic monotonic clock: +1.0s per call."""
    t = {"n": 0.0}

    def tick():
        t["n"] += 1.0
        return t["n"]

    return tick


def _ok_tool():
    return AIMessage(content="", tool_calls=[
        {"name": "ConductResearch", "args": {"research_topic": "x"}, "id": "1", "type": "tool_call"}])


def _no_tool():
    return AIMessage(content="just prose, no tool call")


# -- runner: aggregation ------------------------------------------------------
def test_run_cell_validity_and_latency():
    probe = STAGES_BY_NAME["supervisor"]  # tool contract
    cell = asyncio.run(run_cell(
        "nvidia:fake", probe, reps=4,
        build_model=lambda *a, **k: _FakeModel(_ok_tool),
        judge=None, clock=_clock()))
    assert cell.validity_rate == 1.0
    assert cell.n == 4 and cell.n_ok == 4
    assert cell.latency_p50 == 1.0  # each rep is exactly 1.0s by the fake clock


def test_run_cell_counts_invalid_responses():
    probe = STAGES_BY_NAME["supervisor"]
    cell = asyncio.run(run_cell(
        "nvidia:fake", probe, reps=4,
        build_model=lambda *a, **k: _FakeModel(_no_tool),  # never emits a tool call
        judge=None, clock=_clock()))
    assert cell.validity_rate == 0.0
    assert cell.n_ok == 0


def test_run_cell_classifies_and_short_circuits_backend_fatal():
    probe = STAGES_BY_NAME["supervisor"]

    def boom():
        raise RuntimeError("401 unauthorized: invalid api key")  # -> backend_fatal

    cell = asyncio.run(run_cell(
        "nvidia:fake", probe, reps=5,
        build_model=lambda *a, **k: _FakeModel(boom),
        judge=None, clock=_clock()))
    assert cell.backend_fatal is True
    assert cell.n == 1  # stopped after the first backend-fatal error
    assert cell.error_counts.get("backend_fatal") == 1


def test_run_matrix_skips_remaining_stages_after_backend_fatal():
    supervisor = STAGES_BY_NAME["supervisor"]
    researcher = STAGES_BY_NAME["researcher"]

    def boom():
        raise RuntimeError("429 insufficient_quota")  # backend_fatal

    matrix = asyncio.run(run_matrix(
        ["nvidia:dead"], [supervisor, researcher], reps=3,
        build_model=lambda *a, **k: _FakeModel(boom), judge=None, clock=_clock()))
    # First stage failed backend-fatal; the second must be marked skipped, not run.
    assert matrix.cell("nvidia:dead", "supervisor").backend_fatal
    skipped = matrix.cell("nvidia:dead", "researcher")
    assert skipped.skipped_reason and skipped.validity_rate is None


def test_prose_cell_uses_injected_judge():
    probe = STAGES_BY_NAME["final_report"]  # prose contract

    async def fake_judge(stage, task, output, *, build_model):
        return {"score": 8.5, "grounding": 9, "relevance": 8, "coherence": 8, "reason": "ok"}

    cell = asyncio.run(run_cell(
        "nvidia:fake", probe, reps=2,
        build_model=lambda *a, **k: _FakeModel(lambda: AIMessage(content="A full report.")),
        judge=fake_judge, clock=_clock()))
    assert cell.validity_rate == 1.0       # non-empty prose
    assert cell.judge_mean == 8.5          # from the injected judge


# -- recommendation + report --------------------------------------------------
def _two_model_matrix():
    sup = STAGES_BY_NAME["supervisor"]

    def good():
        return _ok_tool()

    def bad():
        return _no_tool()

    behavior = {"nvidia:good": good, "nvidia:bad": bad}
    return asyncio.run(run_matrix(
        ["nvidia:good", "nvidia:bad"], [sup], reps=3,
        build_model=lambda model, **k: _FakeModel(behavior[model]),
        judge=None, clock=_clock()))


def test_best_fit_picks_higher_validity():
    matrix = _two_model_matrix()
    winner = runner.best_fit(matrix, "supervisor", "tool")
    assert winner is not None
    assert winner[0] == "nvidia:good" and winner[1] == 1.0


def test_render_matrix_and_markdown_are_stable():
    matrix = _two_model_matrix()
    table = report.render_matrix(matrix)
    assert "supervisor" in table and "legend:" in table
    md = report.render_markdown(matrix)
    assert "# NVIDIA per-stage role-fit benchmark" in md
    assert "Best fit per stage" in md and "Candidate NVIDIA routing preset" in md
    blob = report.to_json(matrix)
    assert blob["best_fit"][0]["model"] == "nvidia:good"


# -- throttle/infra vs capability (the rate-limit correctness fix) ------------
def test_transient_errors_excluded_from_validity():
    # 1 valid + 1 throttle: the throttle is infra noise, not a capability data point.
    cell = CellResult("m", "s", "tool", [
        RepResult(ok=True, latency_s=1.0),
        RepResult(ok=False, latency_s=0.1, error_class="transient"),
    ])
    assert cell.validity_rate == 1.0
    assert cell.inconclusive is False


def test_all_transient_cell_is_inconclusive_not_zero():
    cell = CellResult("m", "s", "tool", [
        RepResult(ok=False, latency_s=0.1, error_class="transient"),
        RepResult(ok=False, latency_s=0.1, error_class="transient"),
    ])
    assert cell.validity_rate is None   # NOT 0.0 -- rate-limiting != incapability
    assert cell.inconclusive is True


def test_model_fatal_counts_as_real_failure():
    # A model rejecting the request (wrong/removed id) IS a capability failure, counted.
    cell = CellResult("m", "s", "tool", [
        RepResult(ok=True, latency_s=1.0),
        RepResult(ok=False, latency_s=0.1, error_class="model_fatal"),
    ])
    assert cell.validity_rate == 0.5
    assert cell.inconclusive is False


def test_matrix_from_json_roundtrips_and_reaggregates():
    matrix = _two_model_matrix()
    rebuilt = matrix_from_json(report.to_json(matrix))
    assert rebuilt.models == matrix.models
    assert rebuilt.cell("nvidia:good", "supervisor").validity_rate == 1.0
    assert rebuilt.cell("nvidia:bad", "supervisor").validity_rate == 0.0


# -- probe contracts ----------------------------------------------------------
def test_structured_probe_is_valid_accepts_and_rejects():
    from open_deep_research.state import ResearchQuestion

    probe = STAGES_BY_NAME["research_brief"]
    assert probe.is_valid(ResearchQuestion(research_brief="Research India DPI."))
    assert not probe.is_valid(ResearchQuestion(research_brief=""))


def test_tool_probe_is_valid_accepts_and_rejects():
    probe = STAGES_BY_NAME["researcher"]
    assert probe.is_valid(_ok_tool())
    assert not probe.is_valid(_no_tool())


def test_all_probes_build_messages_without_error():
    # Every probe must be able to render its fixture messages (real prompts/profile load).
    for name, probe in STAGES_BY_NAME.items():
        msgs = probe.messages()
        assert msgs and all(getattr(m, "content", "") for m in msgs), name
