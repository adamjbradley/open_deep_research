import asyncio

from open_deep_research import storage
from open_deep_research.factbase.drift import check_drift


def test_check_drift_same_hash_no_drift(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        rid = await storage.preallocate_run(db, "t")
        await storage.finalize_research_run(db, rid, {
            "profile_name": "p", "profile_hash": "hash_A", "status": "completed"})
        d = await check_drift(db, "p", "hash_A")
        assert d["drifted"] is False and d["last_run_hash"] == "hash_A"

    asyncio.run(go())


def test_check_drift_different_hash_drifts(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        rid = await storage.preallocate_run(db, "t")
        await storage.finalize_research_run(db, rid, {
            "profile_name": "p", "profile_hash": "hash_A", "status": "completed"})
        d = await check_drift(db, "p", "hash_B")
        assert d["drifted"] is True and d["last_run_hash"] == "hash_A"

    asyncio.run(go())


def test_check_drift_no_prior_run(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        await storage.preallocate_run(db, "t")
        d = await check_drift(db, "unseen", "whatever")
        assert d["drifted"] is False and d["last_run_hash"] is None

    asyncio.run(go())
