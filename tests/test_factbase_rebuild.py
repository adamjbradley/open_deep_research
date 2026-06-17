import asyncio
import json

import aiosqlite

from open_deep_research import storage
from open_deep_research.factbase import migrations, schema
from open_deep_research.factbase.profile_schema import profile_from_dict
from open_deep_research.factbase.registry_schema import registry_from_dict
from open_deep_research.factbase.registry import SourceRegistry
from open_deep_research.factbase.rebuild import rebuild_structural

REG = SourceRegistry(registry_from_dict({"version": "1", "sources": [
    {"domain": "a.example", "tier": "authoritative"},
    {"domain": "b.example", "tier": "authoritative"},
]}))


async def _seed_source(conn, url):
    cur = await conn.execute(
        "INSERT INTO source (url_or_domain, tier, flags_json) VALUES (?,?,?)", (url, "authoritative", "[]"))
    return cur.lastrowid


async def _seed_fact(conn, *, property_name, quals, value, source_id, tuple_key, as_of=2024):
    await conn.execute(
        "INSERT INTO fact (tuple_key, instance_key, property_name, qualifiers_json, as_of, value, "
        "canonical_value, source_id, admission, lifecycle, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tuple_key, "india", property_name, json.dumps(quals), as_of, value, value, source_id,
         "trusted", "current", "now"))


def test_dropping_identity_qualifier_collapses_tuples_and_opens_conflict(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            sa = await _seed_source(conn, "https://a.example/x")
            sb = await _seed_source(conn, "https://b.example/y")
            await _seed_fact(conn, property_name="scheme_status", quals={"basis": "de_jure"},
                             value="operational", source_id=sa, tuple_key="OLD_TK_1")
            await _seed_fact(conn, property_name="scheme_status", quals={"basis": "de_facto"},
                             value="mandatory", source_id=sb, tuple_key="OLD_TK_2")
            await conn.commit()

            new_prof = profile_from_dict({"entity_type": "country", "version": "2", "properties": [
                {"name": "scheme_status", "kind": "enum",
                 "value_enum": ["operational", "mandatory", "announced", "piloting"]}]})

            stats = await rebuild_structural(conn, new_prof, REG)
            assert stats["tuple_keys_changed"] == 2
            assert stats["conflicts_opened"] == 1
            assert stats["demoted"] == 2

            cur = await conn.execute("SELECT COUNT(*) FROM conflict WHERE status='open'")
            assert (await cur.fetchone())[0] == 1
            cur = await conn.execute("SELECT DISTINCT tuple_key FROM fact WHERE soft_deleted_at IS NULL")
            assert len(await cur.fetchall()) == 1
            cur = await conn.execute("SELECT COUNT(*) FROM fact WHERE admission='trusted'")
            assert (await cur.fetchone())[0] == 0

    asyncio.run(go())


def test_property_remove_orphan_policy(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            s = await _seed_source(conn, "https://a.example/x")
            await _seed_fact(conn, property_name="gone_prop", quals={}, value="v",
                             source_id=s, tuple_key="TK")
            await conn.commit()
            prof = profile_from_dict({"entity_type": "country", "version": "1",
                                      "properties": [{"name": "kept", "kind": "name"}]})

            stats = await rebuild_structural(conn, prof, REG, on_removed="retain")
            assert stats["orphaned"] == 1
            cur = await conn.execute("SELECT soft_deleted_at FROM fact WHERE property_name='gone_prop'")
            assert (await cur.fetchone())[0] is None

            stats = await rebuild_structural(conn, prof, REG, on_removed="soft_delete")
            cur = await conn.execute("SELECT soft_deleted_at FROM fact WHERE property_name='gone_prop'")
            assert (await cur.fetchone())[0] is not None

    asyncio.run(go())


def test_rename_map_moves_facts_to_new_property(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            s = await _seed_source(conn, "https://a.example/x")
            await _seed_fact(conn, property_name="old_name", quals={}, value="v",
                             source_id=s, tuple_key="TK")
            await conn.commit()
            prof = profile_from_dict({"entity_type": "country", "version": "1",
                                      "properties": [{"name": "new_name", "kind": "name"}]})
            stats = await rebuild_structural(conn, prof, REG, rename={"old_name": "new_name"})
            assert stats["orphaned"] == 0
            cur = await conn.execute("SELECT property_name FROM fact WHERE id=1")
            assert (await cur.fetchone())[0] == "new_name"

    asyncio.run(go())


def test_rebuild_soft_deletes_values_that_no_longer_validate(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            sa = await _seed_source(conn, "https://a.example/x")
            # Old profile allowed the 'multi' member; seed a now-stale value + a valid sibling.
            await _seed_fact(conn, property_name="biometric_capture", quals={},
                             value="multi", source_id=sa, tuple_key="TK_STALE")
            await _seed_fact(conn, property_name="biometric_capture", quals={},
                             value="fingerprint, iris", source_id=sa, tuple_key="TK_OK")
            await conn.commit()

            new_prof = profile_from_dict({"entity_type": "country", "version": "2", "properties": [
                {"name": "biometric_capture", "kind": "enum", "multi": True,
                 "value_enum": ["photo", "fingerprint", "iris", "face"]}]})

            stats = await rebuild_structural(conn, new_prof, REG)
            assert stats["invalidated"] == 1

            stale = await (await conn.execute(
                "SELECT soft_deleted_at FROM fact WHERE value='multi'")).fetchone()
            assert stale[0] is not None  # soft-deleted
            ok = await (await conn.execute(
                "SELECT soft_deleted_at FROM fact WHERE value='fingerprint, iris'")).fetchone()
            assert ok[0] is None         # valid sibling survives

    asyncio.run(go())
