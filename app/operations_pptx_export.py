"""Render generated project reports as validated, editable PPTX files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Literal


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RENDERER_PATH = PROJECT_ROOT / "scripts" / "render_operations_report_pptx.mjs"
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
ReportType = Literal["weekly_tracking", "closure_report"]


class OperationsPptxExportError(RuntimeError):
    """Raised when report rendering or PPTX validation fails."""


def _configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value) if value else default


def export_operations_report_pptx(
    report_type: ReportType,
    report_markdown: str,
    destination: str | Path,
) -> Path:
    """Turn a complete generated report into a readable editable slide deck."""

    if report_type not in {"weekly_tracking", "closure_report"}:
        raise ValueError("报告类型无效。")
    if not isinstance(report_markdown, str) or not report_markdown.strip():
        raise ValueError("报告正文不能为空。")
    if len(report_markdown) > 40000:
        raise ValueError("报告正文过长，无法导出 PPTX。")
    if not report_markdown.lstrip().startswith("# ") or "\n## " not in report_markdown:
        raise ValueError("请先生成完整的项目报告，再导出 PPTX。")
    if not RENDERER_PATH.is_file():
        raise OperationsPptxExportError("项目报告 PPTX 渲染器不存在。")

    node = _configured_path("PRESENTATION_NODE", DEFAULT_NODE)
    node_modules = _configured_path("PRESENTATION_NODE_MODULES", DEFAULT_NODE_MODULES)
    skill_dir = _configured_path("PRESENTATIONS_SKILL_DIR", DEFAULT_SKILL_DIR)
    runtime_python = _configured_path("RUNTIME_PYTHON", DEFAULT_PYTHON)
    if not node.is_file() or not node_modules.is_dir() or not skill_dir.is_dir():
        raise OperationsPptxExportError("PPTX 导出运行环境未就绪。")

    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    file_stem = "project_weekly_report" if report_type == "weekly_tracking" else "cooperation_summary"
    environment = os.environ.copy()
    environment.update({
        "PRESENTATION_NODE_MODULES": str(node_modules),
        "PRESENTATIONS_SKILL_DIR": str(skill_dir),
        "RUNTIME_NODE": str(node),
        "RUNTIME_NODE_MODULES": str(node_modules),
        "RUNTIME_PYTHON": str(runtime_python),
        "OPERATIONS_PPTX_WORKSPACE": str(PROJECT_ROOT),
    })

    with tempfile.TemporaryDirectory(prefix=".operations-pptx-export-", dir=PROJECT_ROOT) as temp_dir:
        work_dir = Path(temp_dir)
        input_path = work_dir / "report.json"
        validated_output = work_dir / "output" / f"{file_stem}.pptx"
        input_path.write_text(json.dumps({
            "report_type": report_type,
            "report_markdown": report_markdown.strip(),
        }, ensure_ascii=False), encoding="utf-8")
        command = [
            str(node), str(RENDERER_PATH),
            "--input", str(input_path),
            "--output", str(validated_output),
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
            diagnostics = (completed.stderr or completed.stdout or "").strip()[-2000:]
            error = OperationsPptxExportError("项目报告 PPTX 生成或校验失败。")
            if diagnostics:
                raise error from RuntimeError(diagnostics)
            raise error
        if (
            not validated_output.is_file()
            or validated_output.stat().st_size < 4
            or validated_output.read_bytes()[:2] != b"PK"
        ):
            output.unlink(missing_ok=True)
            raise OperationsPptxExportError("项目报告渲染器未生成有效 PPTX 文件。")
        shutil.copyfile(validated_output, output)

    if not output.is_file() or output.stat().st_size < 4 or output.read_bytes()[:2] != b"PK":
        output.unlink(missing_ok=True)
        raise OperationsPptxExportError("项目报告 PPTX 文件复制失败。")
    return output
