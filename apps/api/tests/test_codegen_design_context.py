import pytest

from agents.code_agent import CodeGenerationAgent
from pipelines.codegen_design import codegen_design_context
from pipelines.resolved_drone import resolve_drone
from tests.test_resolved_drone import inputs


def state():
    design, parts = inputs(payload=100)
    design.battery.capacity_mah = 34000
    spec = resolve_drone(design, parts)
    return dict(
        design_tree=design.model_dump(),
        selected_components=parts.model_dump(),
        bom_status="selected",
        resolved_drone_spec=spec.model_dump(),
        resolved_drone_sha256=spec.sha256,
    )


def test_catalog_replaces_generated_battery_without_mutating_design():
    data = state()
    context = codegen_design_context(data)
    assert (
        context["battery"]["capacity_mah"] == data["selected_components"]["battery"]["capacity_mah"]
    )
    assert data["design_tree"]["battery"]["capacity_mah"] == 34000
    assert context["payload"]["installed_weight_g"] == 100
    summary = CodeGenerationAgent.__new__(CodeGenerationAgent)._summarize_design(context)
    assert "payload_g=100" in summary
    assert "structural_only" in summary
    assert '"flight_ready": false' in summary
    assert data["resolved_drone_sha256"] in summary


@pytest.mark.parametrize("change", ["battery", "payload", "missing_bom", "missing_model"])
def test_changed_or_missing_model_context_is_rejected(change):
    data = state()
    if change == "battery":
        data["selected_components"]["battery"]["capacity_mah"] += 100
    elif change == "payload":
        data["design_tree"]["payload"]["installed_weight_g"] += 100
    elif change == "missing_bom":
        data["selected_components"] = None
    else:
        data["resolved_drone_spec"] = None
    with pytest.raises(ValueError):
        codegen_design_context(data)


def test_legacy_context_is_explicitly_unverified():
    context = codegen_design_context({"design_tree": {"topology": "quadcopter"}})
    summary = CodeGenerationAgent.__new__(CodeGenerationAgent)._summarize_design(context)
    assert "validation_scope=unverified_design" in summary


@pytest.mark.asyncio
async def test_node_passes_resolved_context_to_generator(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from graph.nodes import generate_code

    data = state()
    generate = AsyncMock(return_value={"code": "# test only", "gsce_config": {}})
    monkeypatch.setattr(
        "agents.code_agent.CodeGenerationAgent",
        Mock(return_value=Mock(generate_code=generate)),
    )
    result = await generate_code(data)
    assert result["code_status"] == "generated"
    context = generate.call_args.kwargs["design_tree"]
    assert (
        context["battery"]["capacity_mah"] == data["selected_components"]["battery"]["capacity_mah"]
    )
    assert context["catalog_context"]["resolved_drone_sha256"] == data["resolved_drone_sha256"]


@pytest.mark.asyncio
async def test_node_clears_old_code_without_calling_generator_on_changed_bom(monkeypatch):
    from unittest.mock import Mock
    from graph.nodes import generate_code

    data = state()
    data["selected_components"]["battery"]["capacity_mah"] += 100
    data["generated_code"] = "# stale"
    data["gsce_config"] = {"old": True}
    factory = Mock()
    monkeypatch.setattr("agents.code_agent.CodeGenerationAgent", factory)
    result = await generate_code(data)
    factory.assert_not_called()
    assert result["code_status"] == "rejected"
    assert result["generated_code"] == ""
    assert result["gsce_config"] == {}
