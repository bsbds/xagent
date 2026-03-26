"""
Data models for DAG plan-execute pattern.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class StepStatus(Enum):
    """Status of a plan step"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExecutionPhase(Enum):
    """Current execution phase"""

    PLANNING = "planning"
    EXECUTING = "executing"
    CHECKING = "checking"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    PLAN_EXTENSION = "plan_extension"


class StepKind(Enum):
    """Semantic kind of a plan step."""

    NORMAL = "normal"
    MAP = "map"


@dataclass
class StepInjection:
    """Pre/post injection hooks for a step"""

    pre_hook: Optional[Callable[[str, Dict[str, Any]], str]] = None
    post_hook: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None


@dataclass
class CollectionOutputRef:
    """Reference to the step result field containing map items."""

    step_id: str
    field: str

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "field": self.field}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CollectionOutputRef":
        return cls(step_id=data["step_id"], field=data["field"])


@dataclass
class MapSpec:
    """Specification for a map step."""

    collection_plan: "ExecutionPlan"
    collection_output: CollectionOutputRef
    item_binding: str
    worker_plan: "ExecutionPlan"
    chunk_size: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "collection_plan": self.collection_plan.to_dict(),
            "collection_output": self.collection_output.to_dict(),
            "item_binding": self.item_binding,
            "chunk_size": self.chunk_size,
            "worker_plan": self.worker_plan.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MapSpec":
        return cls(
            collection_plan=ExecutionPlan.from_dict(data["collection_plan"]),
            collection_output=CollectionOutputRef.from_dict(data["collection_output"]),
            item_binding=data["item_binding"],
            chunk_size=data.get("chunk_size", 1),
            worker_plan=ExecutionPlan.from_dict(data["worker_plan"]),
        )


@dataclass
class ExecutionNode:
    """Runtime execution tree node."""

    node_id: str
    node_type: str
    status: str
    parent_node_id: Optional[str] = None
    runtime_step_id: Optional[str] = None
    template_step_id: Optional[str] = None
    name: Optional[str] = None
    bindings: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    children: List["ExecutionNode"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "status": self.status,
            "parent_node_id": self.parent_node_id,
            "runtime_step_id": self.runtime_step_id,
            "template_step_id": self.template_step_id,
            "name": self.name,
            "bindings": self.bindings,
            "result": self.result,
            "error": self.error,
            "children": [
                child.to_dict() if isinstance(child, ExecutionNode) else child
                for child in self.children
            ],
        }


@dataclass
class PlanStep:
    """A single step in the execution plan with dependency support"""

    id: str
    name: str
    description: str
    tool_names: List[str] = field(
        default_factory=list
    )  # List of tools available for this step
    dependencies: List[str] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    error_traceback: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    injection: Optional[StepInjection] = None
    context: Dict[str, Any] = field(default_factory=dict)
    difficulty: str = "hard"  # easy, hard
    # Conditional branching support
    conditional_branches: Dict[str, str] = field(default_factory=dict)
    # Format: {"branch_key": "next_step_id"}
    # Example: {"human": "human_response_step", "kb": "kb_search_step"}
    required_branch: Optional[str] = None
    # If set, this step only executes if parent selected this branch
    step_kind: StepKind = StepKind.NORMAL
    map_spec: Optional[MapSpec] = None

    @property
    def is_conditional(self) -> bool:
        """Check if this is a conditional node (branching point)"""
        return bool(self.conditional_branches)

    def get_available_tools(self) -> List[str]:
        """Get all tool names available for this step"""
        return self.tool_names.copy()

    def can_execute(
        self,
        completed_steps: Set[str],
        skipped_steps: Optional[Set[str]] = None,
        active_branches: Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        Check if this step can be executed based on dependencies and active branches

        Args:
            completed_steps: Set of completed step IDs
            skipped_steps: Set of skipped step IDs
            active_branches: Dict mapping conditional node IDs to selected branch keys
                              Format: {"parent_step_id": "selected_branch_key"}
        """
        if skipped_steps is None:
            skipped_steps = set()
        if active_branches is None:
            active_branches = {}

        # Check dependencies
        if not all(
            dep_id in completed_steps or dep_id in skipped_steps
            for dep_id in self.dependencies
        ):
            return False

        # Check if this step is on an active branch
        if self.required_branch is not None and active_branches:
            # This step requires a specific branch
            # Check each parent (dependency) to see if it's a conditional node
            for dep_id in self.dependencies:
                selected_branch = active_branches.get(dep_id)
                if selected_branch is not None:
                    # Parent is a conditional node with a selected branch
                    if selected_branch != self.required_branch:
                        return False  # Not on the active branch, skip

        return True

    def _find_step_by_id(self, step_id: str) -> Optional["PlanStep"]:
        """Helper to find a step by ID (will be set by ExecutionPlan)"""
        # This is a placeholder - actual implementation will be in ExecutionPlan
        return None

    def set_dependency_context(self, dependency_results: Dict[str, Any]) -> None:
        """Set context from dependency step results"""
        self.context = dependency_results.copy()

    def to_dict(self) -> Dict[str, Any]:
        """Convert step to dictionary for serialization"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tool_names": self.tool_names,
            "dependencies": self.dependencies,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "error_type": self.error_type,
            "error_traceback": self.error_traceback,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "context": self.context,
            "difficulty": self.difficulty,
            "conditional_branches": self.conditional_branches,
            "required_branch": self.required_branch,
            "is_conditional": self.is_conditional,
            "step_kind": self.step_kind.value,
            "map_spec": self.map_spec.to_dict() if self.map_spec else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanStep":
        started_at = data.get("started_at")
        completed_at = data.get("completed_at")
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            tool_names=data.get("tool_names", []),
            dependencies=data.get("dependencies", []),
            status=StepStatus(data.get("status", StepStatus.PENDING.value)),
            result=data.get("result"),
            error=data.get("error"),
            error_type=data.get("error_type"),
            error_traceback=data.get("error_traceback"),
            started_at=datetime.fromisoformat(started_at) if started_at else None,
            completed_at=datetime.fromisoformat(completed_at) if completed_at else None,
            context=data.get("context", {}),
            difficulty=data.get("difficulty", "hard"),
            conditional_branches=data.get("conditional_branches", {}),
            required_branch=data.get("required_branch"),
            step_kind=StepKind(data.get("step_kind", StepKind.NORMAL.value)),
            map_spec=MapSpec.from_dict(data["map_spec"])
            if data.get("map_spec")
            else None,
        )


@dataclass
class ExecutionPlan:
    """A DAG-structured execution plan"""

    id: str
    goal: str
    steps: List[PlanStep]
    iteration: int = 1
    created_at: datetime = field(default_factory=datetime.now)
    active_branches: Dict[str, str] = field(default_factory=dict)
    # Tracks which branch was selected at each conditional node
    # Format: {"conditional_step_id": "selected_branch_key"}
    task_name: Optional[str] = None  # A concise, meaningful task name for display

    def get_executable_steps(
        self, completed_steps: Set[str], skipped_steps: Set[str]
    ) -> List[PlanStep]:
        """Get steps that can be executed now (considering active branches)"""
        executable = []
        for step in self.steps:
            if (
                step.status == StepStatus.PENDING
                and step.id not in skipped_steps
                and step.can_execute(
                    completed_steps, skipped_steps, self.active_branches
                )
            ):
                executable.append(step)
        return executable

    def get_step_by_id(self, step_id: str) -> Optional[PlanStep]:
        """Get step by ID"""
        return next((step for step in self.steps if step.id == step_id), None)

    def set_active_branch(self, conditional_step_id: str, branch_key: str) -> None:
        """
        Record which branch was selected at a conditional node

        Args:
            conditional_step_id: The ID of the conditional node
            branch_key: The selected branch key (must be in conditional_branches)
        """
        step = self.get_step_by_id(conditional_step_id)
        if not step:
            raise ValueError(f"Step {conditional_step_id} not found")
        if not step.is_conditional:
            raise ValueError(f"Step {conditional_step_id} is not a conditional node")
        if branch_key not in step.conditional_branches:
            raise ValueError(
                f"Invalid branch key: {branch_key}. Valid keys: {list(step.conditional_branches.keys())}"
            )

        self.active_branches[conditional_step_id] = branch_key

    def is_complete(self) -> bool:
        """Check if all steps are completed or skipped"""
        return all(
            step.status in [StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.FAILED]
            for step in self.steps
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert plan to dictionary for serialization"""
        return {
            "id": self.id,
            "goal": self.goal,
            "task_name": self.task_name,  # Include task_name for display
            "iteration": self.iteration,
            "created_at": self.created_at.isoformat(),
            "steps": [step.to_dict() for step in self.steps],
            "active_branches": self.active_branches,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionPlan":
        created_at = data.get("created_at")
        return cls(
            id=data["id"],
            goal=data["goal"],
            steps=[PlanStep.from_dict(step) for step in data.get("steps", [])],
            iteration=data.get("iteration", 1),
            created_at=datetime.fromisoformat(created_at)
            if created_at
            else datetime.now(),
            active_branches=data.get("active_branches", {}),
            task_name=data.get("task_name"),
        )

    def extend_with_steps(self, additional_steps: List[PlanStep]) -> "ExecutionPlan":
        """Create a new plan with additional steps appended."""
        new_plan = ExecutionPlan(
            id=f"{self.id}_extended_{int(datetime.now().timestamp())}",
            goal=self.goal,
            steps=self.steps + additional_steps,
            iteration=self.iteration + 1,  # Increment iteration for extended plans
            task_name=self.task_name,  # Preserve task_name when extending
        )
        return new_plan


class UserInputMapper:
    """
    Simplified user input to step mapping for DAG execution.
    Tracks which steps are associated with which user inputs.
    """

    def __init__(self) -> None:
        self.step_to_input: Dict[str, str] = {}  # step_id -> input_id

    def add_mapping(self, input_id: str, step_ids: List[str]) -> None:
        """Add mapping between user input and execution steps."""
        for step_id in step_ids:
            self.step_to_input[step_id] = input_id

    def get_input_id_by_step_id(self, step_id: str) -> Optional[str]:
        """Get input ID by step ID."""
        return self.step_to_input.get(step_id)

    def clear_mappings(self) -> None:
        """Clear all mappings."""
        self.step_to_input.clear()


def extract_branch_key_from_final_answer(
    final_answer: str, valid_branches: List[str]
) -> Optional[str]:
    """
    Extract branch key from LLM's final answer.

    The LLM should return the branch key as part of its final answer.
    This function tries multiple extraction strategies.

    Args:
        final_answer: The final answer text from LLM
        valid_branches: List of valid branch keys (e.g., ["human", "kb"])

    Returns:
        The extracted branch key, or None if not found

    Examples:
        >>> extract_branch_key_from_final_answer("Select human branch", ["human", "kb"])
        "human"
        >>> extract_branch_key_from_final_answer("Based on analysis, select: kb", ["human", "kb"])
        "kb"
    """
    import re

    if not final_answer or not valid_branches:
        return None

    final_answer_lower = final_answer.lower()

    # Strategy 1: Look for explicit marker like [BRANCH: human] or {branch: kb}
    marker_pattern = r"\[?\s*branch\s*:\s*([^\]]+)\s*\]?"
    match = re.search(marker_pattern, final_answer_lower)
    if match:
        branch = match.group(1).strip()
        if branch in valid_branches:
            return branch

    # Strategy 2: Look for standalone branch key at the start or end
    for branch in valid_branches:
        branch_lower = branch.lower()
        # Check if line starts with branch key
        if re.match(rf"^\s*{re.escape(branch_lower)}\s*[:\s]", final_answer_lower):
            return branch
        # Check if line ends with branch key
        if re.search(rf"[:\s]\s*{re.escape(branch_lower)}\s*$", final_answer_lower):
            return branch

    # Strategy 3: Word matching - look for branch key as a standalone word
    for branch in valid_branches:
        branch_lower = branch.lower()
        # Look for branch key as a whole word (not part of other words)
        pattern = rf"\b{re.escape(branch_lower)}\b"
        if re.search(pattern, final_answer_lower):
            return branch

    # Strategy 4: Fuzzy matching - check if branch key is contained
    for branch in valid_branches:
        if branch.lower() in final_answer_lower:
            return branch

    return None


# Chat response models for interactive conversations


class InteractionType(str, Enum):
    """Types of user interactions in chat mode"""

    SELECT_ONE = "select_one"
    SELECT_MULTIPLE = "select_multiple"
    TEXT_INPUT = "text_input"
    FILE_UPLOAD = "file_upload"
    CONFIRM = "confirm"
    NUMBER_INPUT = "number_input"


@dataclass
class Interaction:
    """A single interaction field in chat mode"""

    type: InteractionType
    field: Optional[str] = None  # Field identifier for response processing
    label: Optional[str] = None  # Display label
    # Type-specific properties
    options: Optional[List[Dict[str, str]]] = None  # For select_*
    placeholder: Optional[str] = None  # For text/number input
    multiline: Optional[bool] = None  # For text input
    min: Optional[int] = None  # For number input
    max: Optional[int] = None  # For number input
    default: Optional[Any] = None  # Default value
    accept: Optional[List[str]] = None  # For file upload (MIME types)
    multiple: Optional[bool] = None  # For file upload or select_multiple


@dataclass
class ChatResponse:
    """Chat response with optional interactions"""

    message: str  # Main message to display
    interactions: Optional[List[Interaction]] = None  # Optional interaction fields


@dataclass
class PlanGeneratorResult:
    """Result from plan generator - either chat or plan"""

    type: str  # "chat" or "plan"
    chat_response: Optional[ChatResponse] = None
    plan: Optional[ExecutionPlan] = None
