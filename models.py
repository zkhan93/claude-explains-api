from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANALYZER_")

    repos_file: Path = Path("repos.yaml")
    prompts_file: Path = Path("prompts.yaml")
    poll_interval_seconds: int = 5
    max_budget_usd: float = 5.00
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["*"]
    output_dir: Path = Path("output")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class Repo(BaseModel):
    name: str
    path: str


class RepoList(BaseModel):
    repos: list[Repo]


class AnalysisResult(BaseModel):
    content: str
    session_id: str


class QueryResult(BaseModel):
    answer: str
