import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from models import Settings
from parser import parse_stream_jsonl

# Own handler so uvicorn can't silence us
logger = logging.getLogger("codebase-analyzer")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


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
    output_file: Path,
    session_id: str | None = None,
    resume_id: str | None = None,
) -> dict:
    """Fire claude as a detached process, poll until done, parse output file.

    Uses --output-format stream-json --verbose so the output file fills up
    as claude works (tail -f to watch progress). When done, parses the JSONL
    to extract the final result.

    Args:
        settings: App settings.
        cwd: Working directory for claude.
        prompt: The prompt text (passed as CLI argument).
        output_file: Path to write claude's stream-json output.
        session_id: Create/continue a named session.
        resume_id: Resume an existing session.

    Returns:
        dict with keys: result (str), session_id (str), is_error (bool)
    """
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--max-budget-usd",
        str(settings.max_budget_usd),
    ]

    if resume_id:
        cmd.extend(["--resume", resume_id])
    elif session_id:
        cmd.extend(["--session-id", session_id])

    cmd.append(prompt)

    logger.info(
        "Starting claude | cwd=%s session=%s resume=%s prompt_len=%d",
        cwd,
        session_id or "-",
        resume_id or "-",
        len(prompt),
    )
    logger.info("Command: %s", " ".join(cmd[:8]) + " ...")
    logger.info("Output: %s (tail -f to watch progress)", output_file)

    # Redirect stdout to file via raw fd — no pipes to our process
    stderr_file = output_file.with_suffix(".stderr")
    stdout_fd = open(output_file, "w")
    stderr_fd = open(stderr_file, "w")
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout_fd.fileno(),
            stderr=stderr_fd.fileno(),
            env=_claude_env(),
            process_group=0,
        )
    except Exception as exc:
        logger.error("Failed to start claude: %s", exc)
        stdout_fd.close()
        stderr_fd.close()
        return {"result": f"Failed to start claude: {exc}", "session_id": "", "is_error": True}
    finally:
        stdout_fd.close()
        stderr_fd.close()

    logger.info("Claude subprocess started | pid=%s", process.pid)

    # Poll until process exits — no server-side timeout, let the client decide
    wait_task = asyncio.create_task(process.wait())
    elapsed = 0
    try:
        while not wait_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=settings.poll_interval_seconds)
            except asyncio.TimeoutError:
                elapsed += settings.poll_interval_seconds
                logger.info("Claude still running | pid=%s elapsed=%ds", process.pid, elapsed)
    except asyncio.CancelledError:
        logger.warning("Request cancelled — killing claude | pid=%s", process.pid)
        _kill_process_tree(process)
        await wait_task
        raise

    logger.info("Claude finished | pid=%s exit=%s elapsed=%ds", process.pid, process.returncode, elapsed)

    if process.returncode != 0:
        error_msg = ""
        if stderr_file.exists():
            error_msg = stderr_file.read_text(encoding="utf-8", errors="replace").strip()
        logger.error("Claude failed | exit=%s error=%s", process.returncode, error_msg[:500])
        return {
            "result": f"Claude CLI failed (exit {process.returncode}): {error_msg}",
            "session_id": "",
            "is_error": True,
        }

    # Parse the stream-json output
    result = parse_stream_jsonl(output_file)

    if result["is_error"]:
        logger.error("Claude returned error: %s", result["result"][:200])
    else:
        logger.info(
            "Claude success | session=%s result_len=%d",
            result["session_id"] or "-",
            len(result["result"]),
        )

    return result
