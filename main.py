import asyncio
import json
import os
import uuid
from pathlib import Path

import yaml
from fastmcp import FastMCP
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANALYZER_")

    repos_file: Path = Path("repos.yaml")
    prompts_file: Path = Path("prompts.yaml")
    claude_timeout_seconds: int = 600
    max_budget_usd: float = 5.00
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["*"]


settings = Settings(_env_file=".env")

# ---------------------------------------------------------------------------
# Repos & Prompts
# ---------------------------------------------------------------------------


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
# Claude subprocess runner
# ---------------------------------------------------------------------------


def _claude_env() -> dict[str, str]:
    """Build env for claude subprocess, stripping nested-session guard."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    return env


async def run_claude(
    cwd: Path,
    prompt: str,
    session_id: str | None = None,
    resume_id: str | None = None,
) -> dict:
    """Run claude CLI and return parsed JSON response.

    Args:
        cwd: Working directory for claude.
        prompt: The prompt text.
        session_id: Create/continue a named session.
        resume_id: Resume an existing session (mutually exclusive with session_id).

    Returns:
        dict with keys: result (str), session_id (str), is_error (bool)
    """
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--max-budget-usd",
        str(settings.max_budget_usd),
        "--permission-mode",
        "dontAsk",
    ]

    if resume_id:
        cmd.extend(["--resume", resume_id])
    elif session_id:
        cmd.extend(["--session-id", session_id])

    cmd.append(prompt)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_claude_env(),
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.claude_timeout_seconds,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return {"result": "Claude timed out", "session_id": "", "is_error": True}

    if process.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace").strip()
        return {
            "result": f"Claude CLI failed (exit {process.returncode}): {error_msg}",
            "session_id": "",
            "is_error": True,
        }

    raw = stdout.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"result": raw, "session_id": "", "is_error": False}

    return {
        "result": data.get("result", raw),
        "session_id": data.get("session_id", ""),
        "is_error": data.get("is_error", False),
    }


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool
def list_repos() -> str:
    """List all repositories available for analysis.
    Call this first to see which repositories are configured."""
    repos = load_repos()
    lines = [f"- {name}: {path}" for name, path in repos.items()]
    return "Available repositories:\n" + "\n".join(lines)


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


@mcp.tool
async def analyze_repo(repo: str) -> str:
    """Get a comprehensive analysis of a repository's architecture and design.
    Call this at the START of any task involving a repository.
    Returns the project's CLAUDE.md context file.

    If the repo has been analyzed before (CLAUDE.md exists), returns it instantly.
    If not, runs a full analysis (may take a few minutes) to create CLAUDE.md.

    Args:
        repo: Repository name (use list_repos to see available names)
    """
    resolved = _resolve_repo(repo)
    if isinstance(resolved, str):
        return resolved
    repo_path, _ = resolved

    claude_md = repo_path / "CLAUDE.md"

    if claude_md.exists():
        return claude_md.read_text(encoding="utf-8")

    # No CLAUDE.md — run Claude with init prompt to create it
    response = await run_claude(
        cwd=repo_path,
        prompt=load_prompts()["init"],
        session_id=str(uuid.uuid4()),
    )

    if response["is_error"]:
        return f"Analysis failed: {response['result']}"

    # Return the CLAUDE.md that Claude created, or fall back to its output
    if claude_md.exists():
        return claude_md.read_text(encoding="utf-8")
    return response["result"]


@mcp.tool
async def query(repo: str, question: str) -> str:
    """Ask a question about a repository's codebase.
    Use this AFTER calling analyze_repo to ask specific questions.
    Each call creates a new Claude session with the repo's CLAUDE.md as context.

    Args:
        repo: Repository name (use list_repos to see available names)
        question: Your question about the codebase
    """
    resolved = _resolve_repo(repo)
    if isinstance(resolved, str):
        return resolved
    repo_path, _ = resolved

    claude_md = repo_path / "CLAUDE.md"
    if not claude_md.exists():
        return "No analysis found. Call analyze_repo first."

    contents = claude_md.read_text(encoding="utf-8")
    prompt = (
        f"Here is the project context:\n\n{contents}\n\n"
        f"Question: {question}"
    )

    response = await run_claude(
        cwd=repo_path,
        prompt=prompt,
    )

    if response["is_error"]:
        return f"Query failed: {response['result']}"

    return response["result"]


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
