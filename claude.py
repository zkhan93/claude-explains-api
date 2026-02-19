import asyncio
import json
import os
import signal

from pathlib import Path

from models import Settings


def _claude_env() -> dict[str, str]:
    """Build env for claude subprocess, stripping nested-session guard."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    return env


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and all its children via process group."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def run_claude(
    settings: Settings,
    cwd: Path,
    prompt: str,
    session_id: str | None = None,
    resume_id: str | None = None,
) -> dict:
    """Run claude CLI and return parsed JSON response.

    The prompt is piped via stdin to avoid issues with long/multi-line
    prompts being passed as shell arguments.

    Args:
        settings: App settings.
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

    # Prompt is sent via stdin — no CLI arg length or escaping issues
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_claude_env(),
        start_new_session=True,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=prompt.encode("utf-8")),
            timeout=settings.claude_timeout_seconds,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        _kill_process_tree(process)
        await process.communicate()
        label = "timed out" if isinstance(exc, asyncio.TimeoutError) else "cancelled"
        return {"result": f"Claude {label}", "session_id": "", "is_error": True}

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
