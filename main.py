import asyncio
import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANALYZER_")

    cache_dir: Path = Path("cache")
    claude_timeout_seconds: int = 300
    max_budget_usd: float = 1.00
    prompts_file: Path = Path("prompts.yaml")
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings(_env_file=".env")
settings.cache_dir.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def load_prompts(path: Path) -> dict[str, str]:
    with open(path) as f:
        return yaml.safe_load(f)


PROMPTS = load_prompts(settings.prompts_file)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Codebase Analyzer",
    description="Upload a zipped codebase and get AI-powered architectural analysis",
)

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def compute_cache_key(zip_content: bytes, analysis_text: str) -> str:
    h = hashlib.sha256()
    h.update(zip_content)
    h.update(analysis_text.encode("utf-8"))
    return h.hexdigest()


def get_cached_result(cache_key: str) -> dict | None:
    cache_file = settings.cache_dir / f"{cache_key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    return None


def store_cached_result(cache_key: str, result: dict) -> None:
    cache_file = settings.cache_dir / f"{cache_key}.json"
    cache_file.write_text(json.dumps(result), encoding="utf-8")


# ---------------------------------------------------------------------------
# Zip extraction with path-traversal protection
# ---------------------------------------------------------------------------


def safe_extract(zf: zipfile.ZipFile, target_dir: str) -> None:
    target = Path(target_dir).resolve()
    for member in zf.infolist():
        member_path = (target / member.filename).resolve()
        if not str(member_path).startswith(str(target)):
            raise HTTPException(
                status_code=400,
                detail="Zip contains unsafe path traversal",
            )
    zf.extractall(target_dir)


# ---------------------------------------------------------------------------
# Claude CLI runner
# ---------------------------------------------------------------------------


def _claude_env() -> dict[str, str]:
    """Build env for claude subprocess, stripping nested-session guard."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    return env


async def run_claude_analysis(codebase_dir: Path, analysis_angle: str) -> str:
    prompt = PROMPTS["analysis"].format(analysis_angle=analysis_angle)

    cmd = [
        "claude",
        "-p",
        
        "--output-format",
        "text",
        "--max-budget-usd",
        str(settings.max_budget_usd),
        "--permission-mode",
        "dontAsk",
        prompt,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(codebase_dir),
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
        raise HTTPException(status_code=504, detail="Claude analysis timed out")

    if process.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(
            status_code=502,
            detail=f"Claude CLI failed (exit {process.returncode}): {error_msg}",
        )

    return stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/analyze")
async def analyze_codebase(
    file: UploadFile = File(..., description="Zip file of the codebase"),
    analysis: str = Form(
        ..., description="Analysis angle/focus, e.g. 'architecture patterns'"
    ),
):
    # 1. Read zip bytes
    zip_content = await file.read()

    # 2. Validate zip
    if not zipfile.is_zipfile(io.BytesIO(zip_content)):
        raise HTTPException(
            status_code=400, detail="Uploaded file is not a valid zip archive"
        )

    # 3. Check cache
    cache_key = compute_cache_key(zip_content, analysis)
    cached = get_cached_result(cache_key)
    if cached is not None:
        cached["cached"] = True
        return JSONResponse(content=cached, headers={"X-Cache": "HIT"})

    # 4. Extract to temp dir
    tmp_dir = tempfile.mkdtemp(prefix="codebase_")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
            safe_extract(zf, tmp_dir)

        # 5. Run claude analysis
        analysis_result = await run_claude_analysis(Path(tmp_dir), analysis)

        # 6. Build and cache response
        result = {
            "analysis": analysis_result,
            "cache_key": cache_key,
            "analysis_angle": analysis,
            "cached": False,
        }
        store_cached_result(cache_key, result)

        return JSONResponse(content=result, headers={"X-Cache": "MISS"})

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    print(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        timeout_keep_alive=600,
    )
