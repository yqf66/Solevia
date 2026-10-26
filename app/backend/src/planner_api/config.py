from functools import lru_cache
from pathlib import Path
import os
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("SOLEVIA_DATA_DIR", str(BACKEND_ROOT / "data"))).resolve()


def settings_env_file() -> Path:
    return Path(os.environ.get("SOLEVIA_ENV_FILE", str(BACKEND_ROOT / ".env"))).resolve()


DEFAULT_DATABASE_URL = f"sqlite:///{(DATA_ROOT / 'planner.db').as_posix()}"


class Settings(BaseSettings):
    app_env: str = "development"
    model_telemetry_enabled: bool = True
    model_telemetry_dir: str = str(DATA_ROOT / "model-telemetry")
    material_upload_dir: str = str(DATA_ROOT / "material-uploads")
    model_telemetry_max_bytes: int = Field(default=8 * 1024 * 1024, ge=65536, le=64 * 1024 * 1024)
    model_telemetry_backups: int = Field(default=3, ge=1, le=8)
    database_url: str = DEFAULT_DATABASE_URL
    llm_provider: Literal["fake", "deepseek", "openai_compatible", "codex"] = "fake"
    solevia_kernel: Literal["auto", "api", "codex"] = "auto"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.deepseek.com"
    llm_proxy_url: str = ""
    llm_model: str = "deepseek-v4-flash"
    llm_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = (
        "low"
    )
    # Fact extraction and schema compilation do not need to re-author the
    # planning argument. Keep the conversational/DEEP reasoning policy separate.
    llm_analysis_reasoning_effort: Literal["none", "low", "medium", "high"] = "none"
    llm_timeout_seconds: float = Field(default=45, gt=0, le=180)
    codex_model: str = ""
    codex_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] = "low"
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_output_tokens: int = Field(default=3000, ge=256, le=16000)
    # Research has its own bounded model calls. A stalled gate or reflection
    # should not consume an entire research run.
    research_decision_timeout_seconds: float = Field(default=45, gt=0, le=180)
    research_reflection_timeout_seconds: float = Field(default=45, gt=0, le=180)
    # Optional override for decision/reflection only; other model nodes retain
    # their existing output budgets. None retains the existing 3000-token cap.
    research_model_max_output_tokens: int | None = Field(default=None, ge=256, le=16000)
    web_search_provider: Literal["disabled", "open_websearch"] = "disabled"
    web_research_enabled: bool = False
    web_search_timeout_seconds: float = Field(default=65, gt=0, le=120)
    web_search_max_results: int = Field(default=15, ge=1, le=50)
    web_search_base_url: str = "http://127.0.0.1:3211"
    # Bing is already routed to cn.bing.com by Open-WebSearch. Keep the
    # default pool small and stable; site-specific engines are opt-in through
    # RESEARCH_ENGINE_POOL instead of being attempted on every query.
    research_engine_pool: str = "bing,duckduckgo,csdn"
    research_max_engines_per_query: int = Field(default=3, ge=1, le=6)
    research_max_subqueries: int = Field(default=12, ge=1, le=12)
    research_max_queries_per_round: int = Field(default=3, ge=1, le=8)
    research_max_rounds: int = Field(default=5, ge=1, le=5)
    research_max_queries: int = Field(default=12, ge=1, le=20)
    research_results_per_query: int = Field(default=12, ge=1, le=20)
    research_max_sources: int = Field(default=15, ge=1, le=50)
    research_max_sources_per_domain: int = Field(default=3, ge=1, le=10)
    research_min_relevance_score: float = Field(default=18, ge=0, le=100)
    research_max_read_pages: int = Field(default=8, ge=1, le=20)
    research_max_pages_per_round: int = Field(default=4, ge=1, le=10)
    research_read_concurrency: int = Field(default=3, ge=1, le=6)
    research_max_page_chars: int = Field(default=8000, ge=1000, le=30000)
    research_max_total_page_chars: int = Field(default=30000, ge=4000, le=100000)
    research_browser_fallback_enabled: bool = True
    research_timeout_seconds: float = Field(default=300, gt=30, le=900)
    research_worker_enabled: bool = True
    research_worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    research_worker_lease_seconds: int = Field(default=300, ge=30, le=3600)
    research_max_recovery_attempts: int = Field(default=3, ge=1, le=10)
    material_max_file_bytes: int = Field(default=100 * 1024 * 1024, ge=1024 * 1024)
    material_max_archive_uncompressed_bytes: int = Field(
        default=256 * 1024 * 1024, ge=16 * 1024 * 1024
    )
    material_chunk_chars: int = Field(default=1800, ge=400, le=8000)
    material_chunk_overlap_chars: int = Field(default=180, ge=0, le=1000)
    material_max_extracted_chars: int = Field(default=25_000_000, ge=10_000, le=100_000_000)
    material_processing_worker_enabled: bool = True
    material_processing_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    material_processing_lease_seconds: int = Field(default=1800, ge=30, le=86400)
    material_processing_max_attempts: int = Field(default=3, ge=1, le=10)
    material_parse_timeout_seconds: float = Field(default=900, ge=1, le=3600)
    material_parse_memory_mb: int = Field(default=4096, ge=512, le=32768)
    material_ocr_enabled: bool = True
    material_ocr_max_pages: int = Field(default=100, ge=1, le=1000)
    material_ocr_dpi: int = Field(default=144, ge=72, le=240)
    material_ocr_min_confidence: float = Field(default=0.6, ge=0, le=1)
    material_mineru_enabled: bool = False
    material_mineru_mode: str = Field(default="AUTO", pattern="^(AUTO|FORCE)$")
    material_mineru_tier: str = Field(default="standard", pattern="^(basic|standard|advanced)$")
    material_mineru_auto_threshold: int = Field(default=2, ge=1, le=6)
    material_mineru_python: str = ""
    material_mineru_home: str = ""
    material_mineru_timeout_seconds: float = Field(default=600, ge=1, le=3600)
    material_mineru_max_pages: int = Field(default=100, ge=1, le=1000)
    material_semantic_enabled: bool = False
    material_semantic_prewarm: bool = False
    knowledge_enabled: bool = False
    knowledge_vectors_enabled: bool = True
    # Vector persistence is independently switchable so Milvus can be
    # backfilled and compared without changing the SQLite source of truth.
    knowledge_vector_backend: Literal["FAISS", "MILVUS"] = "FAISS"
    knowledge_milvus_uri: str = "http://127.0.0.1:19530"
    knowledge_milvus_token: SecretStr = SecretStr("")
    knowledge_milvus_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    knowledge_milvus_collection_prefix: str = Field(default="planner_knowledge", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    knowledge_max_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    knowledge_max_documents: int = Field(default=1000, ge=1, le=10000)
    knowledge_max_chunks: int = Field(default=10000, ge=1, le=100000)
    knowledge_context_chars: int = Field(default=6000, ge=1000, le=12000)
    material_embedding_model: str = "intfloat/multilingual-e5-small"
    material_embedding_revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    material_embedding_cache_dir: str = str(DATA_ROOT / "embedding-models")
    material_embedding_max_chunks: int = Field(default=5000, ge=1, le=20000)
    material_semantic_min_similarity: float = Field(default=0.75, ge=0, le=1)
    material_retrieval_shared_enabled: bool = True
    material_query_cache_enabled: bool = True
    material_query_cache_ttl_seconds: float = Field(default=300, gt=0, le=3600)
    # Local models stay advisory until a held-out business validation gate is
    # satisfied. Shadow requests use a bounded background queue, never the
    # request's critical path; no cloud endpoint is accepted here.
    local_model_mode: Literal["OFF", "SHADOW"] = "OFF"
    local_model_base_url: str = "http://127.0.0.1:3212"
    local_model_timeout_seconds: float = Field(default=0.75, gt=0, le=3)
    # Opt-in experimental compact planner + fine-tuned per-query preferences.
    research_laya_mode: Literal["OFF", "COMPACT"] = "OFF"
    research_laya_base_url: str = "http://127.0.0.1:3213"
    # Independent web candidate ranking; the primary model owns all planning.
    research_rerank_mode: Literal["OFF", "BGE"] = "BGE"
    # One rollback switch for original passage recall + batched span selection.
    # OFF reranking also prevents web requests from invoking E5.
    research_passage_optimization_enabled: bool = True
    # Optional E5 long-page experiment; separate from document indexing rollout.
    research_passage_semantic_enabled: bool = False
    # Independent answer checking is experimental until semantic calibration
    # and end-to-end answer availability both pass. No unchecked fallback.
    research_answer_verification_mode: Literal["OFF", "ENFORCE"] = "OFF"
    # Independent single-pass boundary check; does not rewrite evidence or answers.
    research_answer_scope_guard_enabled: bool = False
    research_rerank_timeout_seconds: float = Field(default=2, gt=0, le=3)
    # Separate rollout from Laya/shadow collection. Only permutes the already
    # selected evidence set, never filters, truncates, or expands model authority.
    material_rerank_mode: Literal["OFF", "GUARDED"] = "OFF"
    # Dataset capture is separate from inference. CAPTURE appends local,
    # reviewable events; it never changes the production answer.
    local_model_dataset_mode: Literal["OFF", "CAPTURE"] = "OFF"
    local_model_dataset_dir: str = str(DATA_ROOT / "local-model-dataset")
    local_model_dataset_store_text: bool = False
    local_model_dataset_max_rows: int = Field(default=15000, ge=100, le=100000)
    local_model_dataset_max_bytes: int = Field(default=256 * 1024 * 1024, ge=1024 * 1024, le=1024 * 1024 * 1024)

    @property
    def material_embedding_identity(self) -> str:
        return f"{self.material_embedding_model}@{self.material_embedding_revision}"

    light_max_output_tokens: int = Field(default=6000, ge=512, le=16000)
    route_max_output_tokens: int = Field(default=6000, ge=512, le=16000)
    route_timeout_seconds: float = Field(default=120, gt=0, le=300)
    route_max_retries: int = Field(default=1, ge=0, le=3)
    route_explanation_max_output_tokens: int = Field(default=3000, ge=256, le=8000)
    route_explanation_timeout_seconds: float = Field(default=90, gt=0, le=180)
    route_explanation_max_retries: int = Field(default=1, ge=0, le=3)
    plan_outline_max_output_tokens: int = Field(default=4000, ge=1000, le=8000)
    plan_max_output_tokens: int = Field(default=12000, ge=1000, le=32000)
    plan_hybrid_author_max_output_tokens: int = Field(default=6000, ge=1000, le=16000)
    plan_hybrid_compiler_max_output_tokens: int = Field(default=24000, ge=4000, le=32000)
    plan_output_retry_max_tokens: int = Field(default=32000, ge=1000, le=128000)
    # The no-reasoning experiment regressed complex capacity/dependency repair.
    # Keep it configurable for comparisons, but retain low for Hybrid compilation.
    plan_compiler_reasoning_effort: Literal["none", "low", "medium", "high"] = "low"
    plan_compiler_bookkeeping_repair_enabled: bool = True
    plan_compiler_contract_enabled: bool = False
    plan_compiler_field_retry_enabled: bool = True
    # Only keyword rewriting, never evidence-based answers or plan compilation.
    material_query_policy: Literal["STANDARD", "FAST"] = "FAST"
    plan_timeout_seconds: float = Field(default=180, gt=0, le=360)
    plan_deep_timeout_seconds: float = Field(default=900, gt=0, le=1800)
    plan_max_retries: int = Field(default=1, ge=0, le=3)
    plan_outline_max_retries: int = Field(default=2, ge=0, le=3)
    plan_detail_max_retries: int = Field(default=1, ge=0, le=3)
    plan_explanation_max_output_tokens: int = Field(default=3500, ge=256, le=8000)
    plan_explanation_timeout_seconds: float = Field(default=90, gt=0, le=180)
    plan_explanation_max_retries: int = Field(default=1, ge=0, le=3)
    plan_adjustment_max_output_tokens: int = Field(default=4000, ge=512, le=10000)
    plan_adjustment_timeout_seconds: float = Field(default=120, gt=0, le=240)
    plan_adjustment_max_retries: int = Field(default=2, ge=0, le=3)
    plan_adjustment_apply_max_output_tokens: int = Field(default=32000, ge=1000, le=32000)
    plan_adjustment_apply_timeout_seconds: float = Field(default=360, gt=0, le=360)
    plan_adjustment_apply_max_retries: int = Field(default=2, ge=0, le=3)
    day_schedule_max_output_tokens: int = Field(default=2400, ge=256, le=8000)
    day_schedule_timeout_seconds: float = Field(default=60, gt=0, le=180)
    day_schedule_max_retries: int = Field(default=2, ge=0, le=5)
    specialist_agent_max_output_tokens: int = Field(default=4000, ge=256, le=4000)
    specialist_agent_reasoning_effort: Literal["none", "low", "medium", "high"] = "none"
    specialist_agent_timeout_seconds: float = Field(default=75, gt=0, le=180)
    specialist_agent_max_retries: int = Field(default=1, ge=0, le=2)
    multi_agent_max_roles: int = Field(default=3, ge=1, le=5)
    specialist_program_facts_enabled: bool = True
    specialist_context_max_batches: int = Field(default=4, ge=1, le=8)
    specialist_batch_cache_enabled: bool = True
    specialist_batch_cache_ttl_seconds: float = Field(default=600, gt=0, le=3600)
    specialist_batch_max_input_multiplier: float = Field(default=2.5, ge=1, le=8)
    specialist_total_timeout_seconds: float = Field(default=150, gt=0, le=360)
    # Soft targets for model context; hard facts and task contracts are never
    # discarded to meet them. Oversized requests are observable in the manifest.
    context_max_input_chars: int = Field(default=60000, ge=12000, le=400000)
    specialist_context_max_input_chars: int = Field(default=28000, ge=8000, le=120000)
    context_max_user_text_chars: int = Field(default=12000, ge=2000, le=100000)
    context_source_feedback_dedup_enabled: bool = True
    conversation_workspace_message_limit: int = Field(default=100, ge=20, le=500)
    conversation_summary_trigger_chars: int = Field(default=8000, ge=1000, le=50000)
    material_hash_chunk_bytes: int = Field(default=1024 * 1024, ge=64 * 1024, le=16 * 1024 * 1024)
    enable_hybrid_work_items: bool = False
    hybrid_rollout_mode: str = Field(default="OFF", pattern="^(OFF|NEW_PLANS|PERCENTAGE|ALL)$")
    hybrid_rollout_percentage: int = Field(default=0, ge=0, le=100)
    cross_plan_priority_mode: str = Field(
        default="BALANCED", pattern="^(PRIORITY|DEADLINE|BALANCED)$"
    )
    holiday_api_base_url: str = "https://date.nager.at/api/v3/PublicHolidays"
    holiday_api_timeout_seconds: float = Field(default=5, gt=0, le=30)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def model_runtime_configured(self) -> bool:
        return self.llm_provider == "codex" or bool(self.llm_api_key.get_secret_value().strip())

    @model_validator(mode="after")
    def validate_material_chunk_settings(self):
        if self.material_chunk_overlap_chars >= self.material_chunk_chars:
            raise ValueError(
                "MATERIAL_CHUNK_OVERLAP_CHARS must be smaller than MATERIAL_CHUNK_CHARS"
            )
        return self

    @field_validator("database_url")
    @classmethod
    def anchor_relative_sqlite_url(cls, value: str) -> str:
        prefix = "sqlite:///./"
        if not value.startswith(prefix):
            return value
        relative_path = value.removeprefix(prefix)
        database_path = (BACKEND_ROOT / relative_path).resolve()
        return f"sqlite:///{database_path.as_posix()}"

    @field_validator("local_model_base_url", "research_laya_base_url")
    @classmethod
    def require_loopback_local_model(cls, value: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise ValueError("LOCAL_MODEL_BASE_URL must be an HTTP IPv4 loopback origin")
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return load_user_settings(settings_env_file(), migrate=True)


def load_user_settings(path: Path, *, migrate=False, kernel: str | None = None) -> Settings:
    from planner_api.credential_store import read_key

    settings = Settings(_env_file=path)
    selected = kernel or settings.solevia_kernel
    if selected == "auto":
        selected = "codex" if settings.llm_provider == "codex" else os.environ.get("SOLEVIA_DEFAULT_KERNEL", "api")
    if selected == "codex":
        # Codex owns its login. Never read or migrate Solevia API credentials.
        return settings.model_copy(update={"llm_provider": "codex", "llm_api_key": SecretStr(""),
                                           "llm_model": settings.codex_model or (settings.llm_model if settings.llm_provider == "codex" else "")})
    if settings.llm_provider == "codex":
        # Legacy Codex-only profiles have no self-configured API connection yet.
        settings = settings.model_copy(update={"llm_provider": "openai_compatible", "llm_model": ""})
    legacy = settings.llm_api_key.get_secret_value()
    # Explicit process secrets remain process-scoped; never persist them.
    key = read_key(path, legacy, migrate=migrate and not os.environ.get("LLM_API_KEY"))
    return settings.model_copy(update={"llm_api_key": SecretStr(key)})
