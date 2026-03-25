from typing import Any, Dict, List

import pytest

from tests.utils.mock_llm import MockLLM
from xagent.core.agent.pattern.dag_plan_execute.models import (
    CollectionOutputRef,
    ExecutionPlan,
    MapSpec,
    PlanStep,
    StepKind,
    StepStatus,
)
from xagent.core.agent.pattern.dag_plan_execute.plan_executor import PlanExecutor
from xagent.core.agent.pattern.dag_plan_execute.plan_generator import PlanGenerator
from xagent.core.agent.trace import Tracer
from xagent.core.memory.in_memory import InMemoryMemoryStore
from xagent.core.workspace import TaskWorkspace
from xagent.core.agent.exceptions import DAGPlanGenerationError


def build_map_plan() -> ExecutionPlan:
    return ExecutionPlan(
        id="translate_plan",
        goal="translate files",
        steps=[
            PlanStep(
                id="list_files",
                name="List Files",
                description="List files",
            ),
            PlanStep(
                id="translate_each_file",
                name="Translate Each File",
                description="Translate selected files",
                dependencies=["list_files"],
                step_kind=StepKind.MAP,
                map_spec=MapSpec(
                    collection_plan=ExecutionPlan(
                        id="derive_files",
                        goal="derive files",
                        steps=[
                            PlanStep(
                                id="select_files",
                                name="Select Files",
                                description="Select files to translate",
                            )
                        ],
                    ),
                    collection_output=CollectionOutputRef(
                        step_id="select_files", field="files"
                    ),
                    item_binding="file",
                    chunk_size=1,
                    worker_plan=ExecutionPlan(
                        id="per_file_translation",
                        goal="translate one file",
                        steps=[
                            PlanStep(
                                id="translate_file",
                                name="Translate File",
                                description="Translate bound file",
                            )
                        ],
                    ),
                ),
            ),
        ],
    )


def test_map_plan_round_trip_serialization():
    plan = build_map_plan()

    restored = ExecutionPlan.from_dict(plan.to_dict())

    map_step = restored.get_step_by_id("translate_each_file")
    assert map_step is not None
    assert map_step.step_kind == StepKind.MAP
    assert map_step.map_spec is not None
    assert map_step.map_spec.collection_output.step_id == "select_files"
    assert map_step.map_spec.worker_plan.steps[0].id == "translate_file"


def test_map_plan_validation_rejects_bad_collection_output():
    plan = build_map_plan()
    assert plan.steps[1].map_spec is not None
    plan.steps[1].map_spec.collection_output = CollectionOutputRef(
        step_id="missing_step", field="files"
    )

    generator = PlanGenerator(MockLLM())

    with pytest.raises(Exception):
        generator._validate_plan_recursive(plan, tools=[])


def test_planning_prompt_includes_map_instructions():
    generator = PlanGenerator(MockLLM())
    messages = generator._build_planning_prompt(
        goal="translate files",
        iteration=1,
        history=[],
        tools=[],
        context=None,
        skill_context=None,
    )

    system_prompt = messages[0]["content"]
    user_prompt = messages[-1]["content"]

    assert "MAP STEPS" in system_prompt
    assert "step_kind" in user_prompt
    assert "map_spec" in user_prompt
    assert "collection_plan must include: id, goal, task_name, iteration, steps" in user_prompt
    assert "worker_plan must include: id, goal, task_name, iteration, steps" in user_prompt


def test_map_validation_feedback_contains_nested_plan_requirements():
    generator = PlanGenerator(MockLLM())
    error = DAGPlanGenerationError(
        "Map step collection_output references a non-existent collection step",
        context={
            "step_id": "translate_each_file",
            "collection_output_step_id": "select_files",
            "collection_step_ids": ["gather_files"],
        },
    )

    feedback = generator._build_map_validation_feedback(error)

    assert feedback is not None
    assert "MAP PLAN REQUIREMENTS" in feedback
    assert "collection_plan and map_spec.worker_plan must each be full nested plans" in feedback
    assert "collection_output.step_id" in feedback


@pytest.mark.asyncio
async def test_plan_executor_executes_map_step(monkeypatch, tmp_path):
    async def fake_execute_step(
        self: PlanExecutor,
        step: PlanStep,
        tool_map: Dict[str, Any],
        execution_results: List[Dict[str, Any]] | None = None,
        skill_context: str | None = None,
    ) -> Dict[str, Any]:
        if step.id == "list_files":
            return {
                "success": True,
                "files": [
                    {"path": "uploads/a.pdf", "name": "a.pdf"},
                    {"path": "uploads/b.pdf", "name": "b.pdf"},
                ],
            }
        if step.id == "select_files":
            return {
                "success": True,
                "files": [
                    {"path": "uploads/a.pdf", "name": "a.pdf"},
                    {"path": "uploads/b.pdf", "name": "b.pdf"},
                ],
            }
        if step.id == "translate_file":
            bound_file = self._current_bindings["file"]
            return {"success": True, "translated_name": bound_file["name"]}
        raise AssertionError(f"Unexpected step {step.id}")

    monkeypatch.setattr(
        PlanExecutor, "_execute_step_with_react_agent", fake_execute_step
    )

    executor = PlanExecutor(
        llm=MockLLM(),
        tracer=Tracer(),
        workspace=TaskWorkspace(id="test_map_v1", base_dir=str(tmp_path)),
        memory_store=InMemoryMemoryStore(),
    )

    results = await executor.execute_plan(build_map_plan(), tool_map={})

    assert [result["step_id"] for result in results] == [
        "list_files",
        "translate_each_file",
    ]

    map_result = results[-1]["result"]
    assert map_result["success"] is True
    assert map_result["mode"] == "map"
    assert map_result["item_count"] == 2
    assert [item["output"]["steps"][0]["result"]["translated_name"] for item in map_result["results"]] == [
        "a.pdf",
        "b.pdf",
    ]

    map_step = executor._current_plan
    assert map_step is None


@pytest.mark.asyncio
async def test_map_collection_step_receives_output_contract(monkeypatch, tmp_path):
    async def fake_execute_step(
        self: PlanExecutor,
        step: PlanStep,
        tool_map: Dict[str, Any],
        execution_results: List[Dict[str, Any]] | None = None,
        skill_context: str | None = None,
    ) -> Dict[str, Any]:
        if step.id == "list_files":
            return {"success": True, "files": [{"value": 1}, {"value": 2}]}
        if step.id == "select_files":
            contract = self._step_output_contracts.get(step.id)
            assert contract is not None
            assert 'top-level field named "files"' in contract
            assert 'The value of "files" must be a list' in contract
            return {"files": [{"value": 1}, {"value": 2}]}
        if step.id == "translate_file":
            return {"success": True}
        raise AssertionError(f"Unexpected step {step.id}")

    monkeypatch.setattr(
        PlanExecutor, "_execute_step_with_react_agent", fake_execute_step
    )

    executor = PlanExecutor(
        llm=MockLLM(),
        tracer=Tracer(),
        workspace=TaskWorkspace(id="test_map_contract", base_dir=str(tmp_path)),
        memory_store=InMemoryMemoryStore(),
    )

    results = await executor.execute_plan(build_map_plan(), tool_map={})

    assert results[-1]["result"]["success"] is True


@pytest.mark.asyncio
async def test_map_executor_extracts_items_from_react_answer_wrapper(monkeypatch, tmp_path):
    async def fake_execute_step(
        self: PlanExecutor,
        step: PlanStep,
        tool_map: Dict[str, Any],
        execution_results: List[Dict[str, Any]] | None = None,
        skill_context: str | None = None,
    ) -> Dict[str, Any]:
        if step.id == "list_files":
            return {"success": True, "files": [1, 2]}
        if step.id == "select_files":
            return {
                "type": "final_answer",
                "answer": '{"files": [{"value": "33 + 44"}, {"value": "55 + 88"}]}',
                "success": True,
            }
        if step.id == "translate_file":
            bound_file = self._current_bindings["file"]
            return {"success": True, "translated_name": bound_file["value"]}
        raise AssertionError(f"Unexpected step {step.id}")

    monkeypatch.setattr(
        PlanExecutor, "_execute_step_with_react_agent", fake_execute_step
    )

    executor = PlanExecutor(
        llm=MockLLM(),
        tracer=Tracer(),
        workspace=TaskWorkspace(id="test_map_answer_wrapper", base_dir=str(tmp_path)),
        memory_store=InMemoryMemoryStore(),
    )

    results = await executor.execute_plan(build_map_plan(), tool_map={})

    map_result = results[-1]["result"]
    assert map_result["success"] is True
    assert [item["output"]["steps"][0]["result"]["translated_name"] for item in map_result["results"]] == [
        "33 + 44",
        "55 + 88",
    ]
