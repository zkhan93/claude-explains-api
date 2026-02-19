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


REPOS: dict[str, str] = load_yaml(settings.repos_file)
PROMPTS: dict[str, str] = load_yaml(settings.prompts_file)

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
    lines = [f"- {name}: {path}" for name, path in REPOS.items()]
    return "Available repositories:\n" + "\n".join(lines)


@mcp.tool
async def analyze_repo(repo: str) -> str:
    """Get a comprehensive analysis of a repository's architecture and design.
    Call this at the START of any task involving a repository.
    Returns the project's CLAUDE.md context file and a session_id for follow-up questions.

    If the repo has been analyzed before, returns the cached analysis instantly.
    If not, runs a full analysis (may take a few minutes).

    Args:
        repo: Repository name (use list_repos to see available names)
    """
    if repo not in REPOS:
        available = ", ".join(REPOS.keys())
        return f"Unknown repo '{repo}'. Available: {available}"

    repo_path = Path(REPOS[repo])
    if not repo_path.is_dir():
        return f"Repo path does not exist: {repo_path}"

    claude_md = repo_path / "CLAUDE.md"
    session_id = str(uuid.uuid4())

    if claude_md.exists():
        # CLAUDE.md already exists — read it and seed a new session with context
        contents = claude_md.read_text(encoding="utf-8")
        seed_prompt = (
            "You are assisting with this codebase. "
            "Here is the context:\n\n"
            f"{contents}\n\n"
            "Answer follow-up questions about this codebase."
        )
        response = await run_claude(
            cwd=repo_path,
            prompt=seed_prompt,
            session_id=session_id,
        )
        return (
            f"## Analysis for {repo}\n\n"
            f"{contents}\n\n"
            f"---\nsession_id: {response.get('session_id') or session_id}"
        )

    # No CLAUDE.md — run full analysis
    init_prompt = PROMPTS["init"]
    response = await run_claude(
        cwd=repo_path,
        prompt=init_prompt,
        session_id=session_id,
    )

    if response["is_error"]:
        return f"Analysis failed: {response['result']}"

    # Read the CLAUDE.md that claude should have created
    if claude_md.exists():
        contents = claude_md.read_text(encoding="utf-8")
    else:
        # Claude didn't create the file — use its output directly
        contents = response["result"]

    return (
        f"## Analysis for {repo}\n\n"
        f"{contents}\n\n"
        f"---\nsession_id: {response.get('session_id') or session_id}"
    )


@mcp.tool
async def query(session_id: str, question: str) -> str:
    """Ask a follow-up question about a repository within an existing session.
    Use this AFTER calling analyze_repo to ask specific questions.
    Claude retains full context from the analysis session.

    Args:
        session_id: The session ID returned by analyze_repo
        question: Your question about the codebase
    """
    if not session_id or not session_id.strip():
        return "Error: session_id is required. Call analyze_repo first."

    # We need a cwd for the subprocess. Since the session already has context,
    # we use the current directory — claude will resume the session regardless.
    response = await run_claude(
        cwd=Path.cwd(),
        prompt=question,
        resume_id=session_id.strip(),
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
        middleware=[
            (
                CORSMiddleware,
                {
                    "allow_origins": settings.cors_origins,
                    "allow_methods": ["*"],
                    "allow_headers": ["*"],
                },
            ),
        ],
    )
