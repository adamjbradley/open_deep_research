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


def test_runner_isolates_per_country_failure(tmp_path):
    db = str(tmp_path / "fail.db")
    ran = []

    async def fake_run_one(country_name, instance_key, *, profile_name, db_path):
        ran.append(instance_key)
        if instance_key == "NGA":
            raise RuntimeError("simulated failure")
        return "run-" + instance_key

    runner = BatchRunner(profile_name="country_cbdc", db_path=db,
                         concurrency=2, run_one=fake_run_one)
    result = asyncio.run(runner.run(["Nigeria", "Bahamas"]))
    assert sorted(ran) == ["BHS", "NGA"]          # both attempted, NGA's failure didn't abort BHS
    assert result["summary"].get("failed") == 1   # NGA failed in isolation
    assert result["summary"].get("done") == 1     # BHS still completed


def test_runner_retries_failed_on_resume(tmp_path):
    db = str(tmp_path / "retry.db")
    attempts = {"NGA": 0}

    async def flaky_run_one(country_name, instance_key, *, profile_name, db_path):
        if instance_key == "NGA":
            attempts["NGA"] += 1
            if attempts["NGA"] == 1:
                raise RuntimeError("first attempt fails")
        return "run-" + instance_key

    runner = BatchRunner(profile_name="country_cbdc", db_path=db,
                         concurrency=1, run_one=flaky_run_one)
    r1 = asyncio.run(runner.run(["Nigeria"]))
    assert r1["summary"].get("failed") == 1       # first attempt failed
    r2 = asyncio.run(runner.run(["Nigeria"]))      # resume retries the failed item
    assert attempts["NGA"] == 2                    # retried
    assert r2["summary"].get("done") == 1          # succeeded on retry
