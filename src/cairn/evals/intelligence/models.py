"""Versioned experiment observations; missing evidence remains null."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from cairn.core.context import TokenCount
from cairn.evals.models import EvalMetrics, EvalResult, EvalSuiteConfig


class Arm(StrEnum):
    BASELINE = "A"
    GRAPH = "B"
    MEMORY = "C"
    BOTH = "D"

    @property
    def graph(self) -> bool:
        return self in (Arm.GRAPH, Arm.BOTH)

    @property
    def memory(self) -> bool:
        return self in (Arm.MEMORY, Arm.BOTH)


class DiscoveryEvidence(BaseModel):
    path: str
    tool: Literal["read_file", "repo_graph"]
    step: int


class IntelligenceMetrics(EvalMetrics):
    initial_tree: str | None = None
    project_id: str | None = None
    tool_calls: int = 0
    read_file_calls: int = 0
    bash_calls: int = 0
    bash_discovery_calls: int = 0
    graph_query_calls: int = 0
    graph_queries_completed: int = 0
    tool_error_events: int = 0
    tool_denied_events: int = 0
    tool_nonzero_results: int = 0
    bash_timeout_results: int = 0
    graph_incomplete_builds: int = 0
    graph_build_refresh_seconds: float = 0
    repeated_read_file_calls: int = 0
    direct_discovery_evidence: list[DiscoveryEvidence] = Field(default_factory=list)
    memory_fixture_entries: int = 0
    memory_candidates_checked: int = 0
    memory_freshness_counts: dict[str, int] = Field(default_factory=dict)
    memory_selected_ids: list[str] = Field(default_factory=list)
    memory_included_ids: list[str] = Field(default_factory=list)
    memory_dropped_ids: list[str] = Field(default_factory=list)
    memory_selected_events: int = 0
    memory_included_events: int = 0
    memory_budget_drop_events: int = 0
    memory_size_drop_events: int = 0
    memory_ineligible_included_events: int = 0
    memory_relevant_included_events: int = 0
    memory_read_errors: int = 0
    memory_retrieval_freshness_seconds: float = 0
    memory_fixture_seconds: float = 0
    fixture_setup_seconds: float = 0
    agent_elapsed_seconds: float | None = None
    request_token_counts: list[TokenCount] = Field(default_factory=list)
    request_tokens_total: int = 0
    provider_requests: int = 0
    provider_responses: int = 0
    provider_failures: int = 0
    provider_failure_categories: dict[str, int] = Field(default_factory=dict)
    stale_recall_rate: float | None = None
    relevant_memory_precision: float | None = None


class ExperimentConfig(BaseModel):
    dataset_version: Literal["repo-intelligence-v1"] = "repo-intelligence-v1"
    dataset_sha256: str
    source_revision: str | None = None
    execution_kind: Literal["offline", "real"]
    model_identifier: str
    provider_name: str | None = None
    provider_endpoint_sha256: str | None = None
    seed: int = 230028
    repetitions: int = Field(default=1, ge=1, le=3)
    case_names: tuple[str, ...]
    settings: EvalSuiteConfig
    tool_timeout_seconds: float = Field(default=10, gt=0, le=30)
    sandbox: str
    network_policy: Literal["baseline-deny-no-approval"] = "baseline-deny-no-approval"


class ExperimentRun(BaseModel):
    run_id: str
    case_name: str
    arm: Arm
    repetition: int
    order_index: int
    result: EvalResult
    metrics: IntelligenceMetrics


class ExperimentReport(BaseModel):
    schema_version: Literal[1] = 1
    config: ExperimentConfig
    state: Literal["running", "completed", "interrupted", "real_eval_blocked"] = (
        "running"
    )
    runs: list[ExperimentRun] = Field(default_factory=list)
    active_run_id: str | None = None
    active_metrics: IntelligenceMetrics | None = None
    real_provider_status: Literal["PASS", "FAIL", "BLOCKED"] | None = None
