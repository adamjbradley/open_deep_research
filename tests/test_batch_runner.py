import asyncio

from open_deep_research.factbase.batch import BatchRunner


def test_runner_runs_resolved_skips_done_reports_unresolved(tmp_path):
    db = str(tmp_path / "r.db")
    ran = []

    async def fake_run_one(country_name, instance_key, *, profile_name, db_path):
        ran.append(instance_key)
        return "run-" + instance_key  # pretend run_id

    runner = BatchRunner(profile_name="country_cbdc", db_path=db,
                         concurrency=2, run_one=fake_run_one)
    result = asyncio.run(runner.run(["Nigeria", "Bahamas", "Atlantis"]))  # Atlantis unresolved

    assert sorted(ran) == ["BHS", "NGA"]
    assert result["unresolved"] == ["Atlantis"]
    assert result["summary"].get("done") == 2

    # resume: a second run does no work (both done)
    ran.clear()
    result2 = asyncio.run(runner.run(["Nigeria", "Bahamas"]))
    assert ran == []
    assert result2["summary"].get("done") == 2
