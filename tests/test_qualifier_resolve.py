# tests/test_qualifier_resolve.py
import asyncio
from open_deep_research.factbase.qualifier_resolve import resolve_qualifier


def _mk(text):
    async def model_call(prompt):
        return text
    return model_call


def _call(text, allow_inference):
    return asyncio.run(resolve_qualifier(
        value="true", instance_name="Estonia", property_name="dpl",
        qualifier="stage", enum=["enacted", "in_force"], evidence_span="the Act is in force since 2019",
        allow_inference=allow_inference, model_call=_mk(text)))


def test_stated_qualifier_is_returned():
    assert _call('{"value": "in_force", "basis": "stated"}', allow_inference=False) == \
        {"value": "in_force", "basis": "stated"}


def test_inferred_deferred_when_inference_not_allowed():
    assert _call('{"value": "in_force", "basis": "inferred"}', allow_inference=False) is None


def test_inferred_returned_when_allowed():
    assert _call('{"value": "in_force", "basis": "inferred"}', allow_inference=True) == \
        {"value": "in_force", "basis": "inferred"}


def test_value_outside_enum_rejected():
    assert _call('{"value": "repealed", "basis": "stated"}', allow_inference=True) is None


def test_null_value_returns_none():
    assert _call('{"value": null}', allow_inference=True) is None


def test_unparseable_returns_none():
    assert _call('the model rambled with no json', allow_inference=True) is None
