import asyncio

from open_deep_research import storage
from open_deep_research.factbase import dossier, profile


def test_recompute_check_reports_drift_then_clear(tmp_path):
    db = str(tmp_path / "fb.db")
    real_hash = profile.load("country_digital_identity").profile_hash

    async def go():
        rid = await storage.preallocate_run(db, "t")
        await storage.finalize_research_run(db, rid, {
            "profile_name": "country_digital_identity",
            "profile_hash": "STALE", "status": "completed"})
        out = await dossier.run(["recompute", "--check"], db_path=db)
        assert "DRIFT" in out

        rid2 = await storage.preallocate_run(db, "t2")
        await storage.finalize_research_run(db, rid2, {
            "profile_name": "country_digital_identity",
            "profile_hash": real_hash, "status": "completed"})
        out2 = await dossier.run(["recompute", "--check"], db_path=db)
        assert "no drift" in out2

    asyncio.run(go())


def test_recompute_action_runs_on_empty_factbase(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        out = await dossier.run(["recompute"], db_path=db)
        assert "recomputed" in out and "0" in out  # empty fact table -> 0 rows

    asyncio.run(go())
