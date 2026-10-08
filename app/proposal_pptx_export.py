"""Bridge the structured proposal payload to the editable PPTX renderer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RENDERER_PATH = PROJECT_ROOT / "scripts" / "render_proposal_pptx.mjs"
DEFAULT_NODE = Path(
    r"C:\Users\yyds\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
)
DEFAULT_NODE_MODULES = Path(
    r"C:\Users\yyds\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"
)
DEFAULT_SKILL_DIR = Path(
    r"C:\Users\yyds\.codex\plugins\cache\openai-primary-runtime"
    r"\presentations\26.909.12148\skills\presentations"
)
DEFAULT_PYTHON = Path(
    r"C:\Users\yyds\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
)


class ProposalPptxExportError(RuntimeError):
    """Raised when the isolated PPTX renderer cannot produce a verified file."""


def _configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value) if value else default


def export_proposal_pptx(
    proposal: Mapping[str, Any] | None,
    destination: str | Path,
) -> Path:
    """Render and finalize one editable PPTX from structured proposal evidence.

    The payload is written to a short-lived UTF-8 JSON file. Model credentials
    are never copied into it or included in renderer error messages.
    """

    if proposal is not None and not isinstance(proposal, Mapping):
        raise ValueError("方案结构化资料必须是 JSON 对象。")
    if not RENDERER_PATH.is_file():
        raise ProposalPptxExportError("PPTX 渲染器不存在。")

    node = _configured_path("PRESENTATION_NODE", DEFAULT_NODE)
    node_modules = _configured_path("PRESENTATION_NODE_MODULES", DEFAULT_NODE_MODULES)
    skill_dir = _configured_path("PRESENTATIONS_SKILL_DIR", DEFAULT_SKILL_DIR)
    runtime_python = _configured_path("RUNTIME_PYTHON", DEFAULT_PYTHON)
    if not node.is_file() or not node_modules.is_dir() or not skill_dir.is_dir():
        raise ProposalPptxExportError("PPTX 导出运行环境未就绪。")

    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"proposal": dict(proposal or {})}
    environment = os.environ.copy()
    environment.update(
        {
            "PRESENTATION_NODE_MODULES": str(node_modules),
            "PRESENTATIONS_SKILL_DIR": str(skill_dir),
            "RUNTIME_NODE": str(node),
            "RUNTIME_NODE_MODULES": str(node_modules),
            "RUNTIME_PYTHON": str(runtime_python),
            "PROPOSAL_WORKSPACE": str(PROJECT_ROOT),
        }
    )

    with tempfile.TemporaryDirectory(
        prefix=".proposal-pptx-export-",
        dir=PROJECT_ROOT,
    ) as temp_dir:
        work_dir = Path(temp_dir)
        input_path = work_dir / "proposal.json"
        validated_output = work_dir / "output" / "marketing_proposal.pptx"
        input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        command = [
            str(node),
            str(RENDERER_PATH),
            "--input",
            str(input_path),
            "--output",
            str(validated_output),
            "--finalize",
        ]
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            output.unlink(missing_ok=True)
            raise ProposalPptxExportError(
                f"PPTX 渲染失败（退出码 {completed.returncode}）。"
            )
        if (
            not validated_output.is_file()
            or validated_output.stat().st_size < 4
            or validated_output.read_bytes()[:2] != b"PK"
        ):
            output.unlink(missing_ok=True)
            raise ProposalPptxExportError("PPTX 渲染器未生成有效文件。")
        shutil.copyfile(validated_output, output)
    if not output.is_file() or output.stat().st_size < 4 or output.read_bytes()[:2] != b"PK":
        output.unlink(missing_ok=True)
        raise ProposalPptxExportError("PPTX 导出文件复制失败。")
    return output
