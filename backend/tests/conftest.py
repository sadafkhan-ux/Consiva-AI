import os

# Settings has several required fields (real secrets in real deployments) — tests never
# touch a live NVIDIA/Supabase endpoint, so dummy values just need to satisfy validation.
os.environ.setdefault("NVIDIA_API_KEY", "test-key")
os.environ.setdefault("NVIDIA_LLM_MODEL", "test-model")
os.environ.setdefault("NVIDIA_EMBED_MODEL", "test-embed-model")
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/test")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret")
os.environ.setdefault("APP_ENV", "development")
