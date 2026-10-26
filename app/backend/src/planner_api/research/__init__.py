"""Agent-owned web research tools and provider contracts."""

from .context import compile_research_context, estimate_research_units
from .evidence import (
    build_material_passage,
    build_web_passages,
    estimate_source_evidence_density,
    merge_evidence,
    refresh_evidence_metrics,
    validate_evidence_analysis,
)
from .intent import (
    DeepSeekResearchDecisionProvider,
    FakeResearchIntentAnalyzer,
    ModelResearchIntentAnalyzer,
    ResearchDecisionProvider,
    ResearchEvidenceAnalyzer,
    ResearchIntentAnalyzer,
    build_research_decision_provider,
    build_research_intent_analyzer,
)
from .memory import refresh_research_memory
from .orchestrator import ResearchLimits, ResearchOrchestrator, ResearchProgressObserver
from .persistence import ResearchRunService
from .providers import OpenWebSearchProvider, ResearchProviderError
from .service import WebResearchService
from .worker import ResearchWorker

__all__ = [
    "OpenWebSearchProvider",
    "ResearchLimits",
    "ResearchOrchestrator",
    "ResearchProgressObserver",
    "ResearchProviderError",
    "ResearchRunService",
    "ResearchWorker",
    "WebResearchService",
    "compile_research_context",
    "estimate_research_units",
    "build_material_passage",
    "build_web_passages",
    "estimate_source_evidence_density",
    "merge_evidence",
    "refresh_evidence_metrics",
    "validate_evidence_analysis",
    "FakeResearchIntentAnalyzer",
    "DeepSeekResearchDecisionProvider",
    "ModelResearchIntentAnalyzer",
    "ResearchDecisionProvider",
    "ResearchEvidenceAnalyzer",
    "ResearchIntentAnalyzer",
    "build_research_decision_provider",
    "build_research_intent_analyzer",
    "refresh_research_memory",
]
