import logging
import uuid
from pathlib import Path

import yaml
from fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware

from claude import run_claude
from models import AnalysisResult, QueryResult, Repo, RepoList, Settings

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("codebase-analyzer")

settings = Settings(_env_file=".env")
settings.output_dir.mkdir(exist_ok=True)


def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_repos() -> dict[str, str]:
    return load_yaml(settings.repos_file)


def load_prompts() -> dict[str, str]:
    return load_yaml(settings.prompts_file)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("codebase-analyzer")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_repo(repo: str) -> tuple[Path, str] | str:
    """Validate repo name and return (repo_path, repo_name) or an error string."""
    repos = load_repos()
    if repo not in repos:
        available = ", ".join(repos.keys())
        return f"Unknown repo '{repo}'. Available: {available}"
    repo_path = Path(repos[repo])
    if not repo_path.is_dir():
        return f"Repo path does not exist: {repo_path}"
    return repo_path, repo


def _output_file(label: str) -> Path:
    """Create a unique output file path in the output directory."""
    return settings.output_dir / f"{label}-{uuid.uuid4().hex[:8]}.json"


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool
def list_repos() -> RepoList:
    """List all repositories available for analysis.
    Call this first to see which repositories are configured."""
    repos = load_repos()
    return RepoList(repos=[Repo(name=n, path=p) for n, p in repos.items()])


@mcp.tool
async def analyze_repo(repo: str) -> AnalysisResult:
    """Get a comprehensive analysis of a repository's architecture and design.
    Call this at the START of any task involving a repository.
    Returns the project's CLAUDE.md content and a session_id for follow-up queries.

    If the repo has been analyzed before (CLAUDE.md exists), returns it instantly.
    If not, runs a full analysis (may take a few minutes) to create CLAUDE.md.

    Args:
        repo: Repository name (use list_repos to see available names)
    """
    resolved = _resolve_repo(repo)
    if isinstance(resolved, str):
        return AnalysisResult(content=resolved, session_id="")

    repo_path, _ = resolved
    claude_md = repo_path / "CLAUDE.md"
    session_id = str(uuid.uuid4())

    # Always run init prompt — if CLAUDE.md exists, Claude skips re-analysis
    # and returns quickly. Either way we get a real session for follow-up queries.
    cached = claude_md.exists()
    logger.info(
        "%s for '%s', creating session...",
        "CLAUDE.md found" if cached else "No CLAUDE.md",
        repo,
    )

    response = await run_claude(
        settings=settings,
        cwd=repo_path,
        prompt=load_prompts()["init"],
        output_file=_output_file(f"analyze-{repo}"),
        session_id=session_id,
    )

    if response["is_error"]:
        return AnalysisResult(content=f"Analysis failed: {response['result']}", session_id="")

    # Always return CLAUDE.md contents (ignore Claude's response text when cached)
    content = claude_md.read_text(encoding="utf-8") if claude_md.exists() else response["result"]
    return AnalysisResult(
        content=content,
        session_id=response.get("session_id") or session_id,
    )


@mcp.tool
async def query(session_id: str, question: str) -> QueryResult:
    """Ask a follow-up question about a repository within an existing session.
    Use this AFTER calling analyze_repo — pass the session_id it returned.
    Claude resumes the session with full context from the analysis.

    Args:
        session_id: The session_id returned by analyze_repo
        question: Your question about the codebase
    """
    if not session_id or not session_id.strip():
        return QueryResult(answer="session_id is required. Call analyze_repo first.")

    response = await run_claude(
        settings=settings,
        cwd=Path.cwd(),
        prompt=question,
        output_file=_output_file("query"),
        resume_id=session_id.strip(),
    )

    if response["is_error"]:
        return QueryResult(answer=f"Query failed: {response['result']}")

    return QueryResult(answer=response["result"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        middleware=[
            (
                CORSMiddleware,
                [],
                {
                    "allow_origins": settings.cors_origins,
                    "allow_methods": ["*"],
                    "allow_headers": ["*"],
                },
            ),
        ],
    )
