from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # NVIDIA NIM
    nvidia_api_key: str
    nvidia_api_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_llm_model: str
    nvidia_embed_model: str
    nvidia_embed_dimensions: int = 1024

    # Provider selection for the Consent Agent's reasoning/structured-output step
    # ONLY -- embeddings (RAG) always use NVIDIA regardless of this setting, since
    # Groq has no embeddings endpoint at all (confirmed live against its own
    # /v1/models catalog: chat/audio models only). Defaults to "nvidia" so existing
    # deployments/tests that don't set this var keep their current behavior exactly.
    llm_provider: Literal["nvidia", "groq"] = "nvidia"
    # Optional (not required) so an existing .env with only NVIDIA configured still
    # validates fine -- GroqLLMClient itself raises a clear error if constructed
    # without these, rather than Settings() failing to even start.
    groq_api_key: str | None = None
    groq_api_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str | None = None

    # Optional self-hosted primary LLM (e.g. a llama.cpp server) -- tried FIRST when
    # both of these are set; settings.llm_provider (nvidia/groq, above) then becomes
    # the FALLBACK used only if the primary genuinely fails. Neither NVIDIA nor Groq
    # configuration changes at all when this is unset -- existing behavior is
    # unaffected. api_key is optional since a self-hosted server may require no auth
    # at all (confirmed live for the one this was built against).
    llm_primary_base_url: str | None = None
    llm_primary_model: str | None = None
    llm_primary_api_key: str | None = None

    # Supabase / Postgres
    supabase_url: str
    supabase_service_role_key: str
    database_url: str
    supabase_jwt_secret: str

    # Scanner
    scanner_headless: bool = True
    scanner_max_pages: int = 25
    scanner_timeout_seconds: int = 30
    scanner_user_agent: str = "ConsivaConsentAgent/0.1"
    scanner_max_scans_per_org_per_day: int = 50
    # Bounded concurrent page fetching for the pre_consent crawl (moved here from a
    # hardcoded crawler.py constant so ops can tune it per deployment without a code
    # change). Default of 4 is the value already measured live against a real 25-page
    # site (see crawler.py's own docstring) -- not changed, just made configurable.
    scanner_max_concurrent_pages: int = 4
    # India (DPDP) is this product's target compliance audience, so the scanner
    # presents as an Indian-locale browser by default -- this is a request-header/
    # navigator hint only. It does NOT affect network egress IP, and it does NOT make
    # a geo-IP-gated CMP show a banner it wouldn't otherwise show to this server's
    # actual vantage point -- that limitation needs a real India-region proxy/egress,
    # which is a separate infrastructure decision, not something these two settings
    # can substitute for.
    scanner_locale: str = "en-IN"
    scanner_accept_language: str = "en-IN,en;q=0.9"
    # When True, a scan is refused unless the target domain has passed DNS-TXT
    # ownership verification (websites.verified_at set). Default False so the
    # existing self-attestation demo flow keeps working; flip on before any
    # production pilot per the master reference's Critical fix list.
    scanner_require_domain_verification: bool = False

    # RAG
    # Chunks whose pgvector cosine DISTANCE exceeds this are dropped rather than
    # padded into the LLM prompt as fake-relevant context. Real observed distances
    # for genuinely-relevant chunks in live scans sit around 0.77-0.83, so 0.95 is
    # deliberately loose -- it only excludes chunks that are barely related at all.
    # Tune only against test_rag_retrieval_quality.py runs on live infrastructure.
    rag_max_distance: float = 0.95

    # LLM
    # Shared wall-clock budget for one analysis's ENTIRE LLM effort, across all three
    # retry layers (graph validation retry x schema-repair retry x network retry).
    # Without it, the theoretical worst case is 3x3x3 = 27 real NVIDIA calls each with
    # its own 60s timeout. 480s comfortably exceeds the slowest successful run ever
    # observed live (302s during real NVIDIA endpoint degradation) -- a tighter value
    # would have converted that real success into a failure.
    llm_deadline_seconds: int = 480

    # Action Module notifications (master reference §5/§8: "email/alert
    # notifications"). Unset by default -- no notification is ever silently claimed as
    # sent without a real endpoint configured. Point this at a real Slack/Discord/MS
    # Teams "incoming webhook" URL (all three accept a POST-JSON-to-a-URL contract) or
    # any custom endpoint that does the same; transitioning a notification action to
    # "done" performs a real HTTP POST here and only succeeds if that POST succeeds.
    notification_webhook_url: str | None = None
    notification_webhook_timeout_seconds: int = 10

    # App
    # No default: a misconfigured deployment that forgets to set this must fail to
    # start, not silently default to "development" and leave app/api/v1/routes/dev.py's
    # unauthenticated demo-token-minting endpoint open in production.
    app_env: str
    log_level: str = "INFO"

    @property
    def psycopg_database_url(self) -> str:
        """`database_url` uses SQLAlchemy's `postgresql+asyncpg://` driver syntax —
        psycopg (used directly by the LangGraph checkpointer, not through SQLAlchemy)
        doesn't understand the `+asyncpg` qualifier and errors on it. Strip it here
        rather than maintain two separate connection strings in .env."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
