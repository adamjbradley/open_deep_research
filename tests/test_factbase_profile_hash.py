import copy

from open_deep_research.factbase.profile_schema import profile_from_dict

BASE = {
    "entity_type": "country",
    "version": "1",
    "properties": [
        {"name": "scheme_status", "kind": "enum", "description": "x",
         "value_enum": ["a", "b"]},
    ],
}


def test_hash_is_stable_and_present():
    h1 = profile_from_dict(copy.deepcopy(BASE)).profile_hash
    h2 = profile_from_dict(copy.deepcopy(BASE)).profile_hash
    assert isinstance(h1, str) and len(h1) == 64  # sha256 hex
    assert h1 == h2


def test_hash_ignores_description_and_notes_changes():
    other = copy.deepcopy(BASE)
    other["notes"] = "human comment that should not change identity"
    other["properties"][0]["description"] = "a totally different description"
    assert profile_from_dict(other).profile_hash == profile_from_dict(copy.deepcopy(BASE)).profile_hash


def test_hash_changes_on_semantic_change():
    other = copy.deepcopy(BASE)
    other["properties"][0]["value_enum"] = ["a", "b", "c"]  # enum changed -> semantic
    assert profile_from_dict(other).profile_hash != profile_from_dict(copy.deepcopy(BASE)).profile_hash
