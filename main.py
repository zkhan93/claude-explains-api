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

settings = Settings(_env_file=".env")


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

    if claude_md.exists():
        return AnalysisResult(
            content=claude_md.read_text(encoding="utf-8"),
            session_id=session_id,
        )

    # No CLAUDE.md — run Claude with init prompt to create it
    response = await run_claude(
        settings=settings,
        cwd=repo_path,
        prompt=load_prompts()["init"],
        session_id=session_id,
    )

    if response["is_error"]:
        return AnalysisResult(content=f"Analysis failed: {response['result']}", session_id="")

    # Return the CLAUDE.md that Claude created, or fall back to its output
    content = claude_md.read_text(encoding="utf-8") if claude_md.exists() else response["result"]
    return AnalysisResult(
        content=content,
        session_id=response.get("session_id") or session_id,
    )


@mcp.tool
async def query(repo: str, question: str) -> QueryResult:
    """Ask a question about a repository's codebase.
    Use this AFTER calling analyze_repo to ask specific questions.
    Each call creates a new Claude session with the repo's CLAUDE.md as context.

    Args:
        repo: Repository name (use list_repos to see available names)
        question: Your question about the codebase
    """
    resolved = _resolve_repo(repo)
    if isinstance(resolved, str):
        return QueryResult(answer=resolved)

    repo_path, _ = resolved
    claude_md = repo_path / "CLAUDE.md"
    if not claude_md.exists():
        return QueryResult(answer="No analysis found. Call analyze_repo first.")

    contents = claude_md.read_text(encoding="utf-8")
    prompt = (
        f"Here is the project context:\n\n{contents}\n\n"
        f"Question: {question}"
    )

    response = await run_claude(
        settings=settings,
        cwd=repo_path,
        prompt=prompt,
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
