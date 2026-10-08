"""Resolve customer-facing project names to internal project identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from app.operations_service import OperationsService


ResolutionState = Literal["resolved", "missing_name", "not_found", "ambiguous"]

_STATUS_LABELS = {
    "active": "进行中",
    "closing": "收尾中",
    "completed": "已完成",
    "paused": "已暂停",
    "cancelled": "已取消",
}


@dataclass(frozen=True, slots=True)
class ProjectResolution:
    state: ResolutionState
    payload: dict[str, Any]
    candidates: tuple[dict[str, Any], ...] = ()


def resolve_project_payload(
    service: OperationsService,
    values: Mapping[str, Any],
    *,
    nested_key: str,
) -> ProjectResolution:
    """Resolve a merchant/project description while preserving explicit-ID compatibility."""

    payload = dict(values)
    nested_value = payload.get(nested_key)
    nested = dict(nested_value) if isinstance(nested_value, Mapping) else None
    project_id = payload.get("project_id") or (nested or {}).get("project_id")
    if project_id is not None:
        payload["project_id"] = project_id
        if nested is not None:
            nested["project_id"] = project_id
            payload[nested_key] = nested
        return ProjectResolution("resolved", payload)

    merchant_name = str(payload.get("merchant_name") or "").strip()
    if not merchant_name:
        return ProjectResolution("missing_name", payload)

    matches = service.find_projects(
        merchant_name=merchant_name,
        project_name=str(payload.get("project_name") or "").strip(),
        store_scope=str(payload.get("store_scope") or "").strip(),
    )
    if not matches:
        return ProjectResolution("not_found", payload)
    if len(matches) > 1:
        return ProjectResolution("ambiguous", payload, tuple(matches))

    project_id = int(matches[0]["id"])
    payload["project_id"] = project_id
    if nested is not None:
        nested["project_id"] = project_id
        payload[nested_key] = nested
    return ProjectResolution("resolved", payload)


def clarification_payload(
    resolution: ProjectResolution,
    *,
    merchant_name: str,
) -> tuple[str, dict[str, Any]]:
    """Build an actionable clarification without exposing internal IDs in its text."""

    if resolution.state == "missing_name":
        return "请填写商家名称后再发送。", {"needs_clarification": True}
    if resolution.state == "not_found":
        return (
            f"没有找到与“{merchant_name}”匹配的项目，请检查商家名称后重新发送。",
            {"needs_clarification": True},
        )

    candidates = [_candidate_payload(item) for item in resolution.candidates]
    lines = [f"找到多个“{merchant_name}”相关项目，请选择本次要处理的项目：", ""]
    lines.extend(
        f"{index}. {_candidate_label(candidate)}"
        for index, candidate in enumerate(candidates, start=1)
    )
    lines.extend(("", "请直接回复“选择第1个”或“选择第2个”。"))
    return "\n".join(lines), {
        "needs_clarification": True,
        "requires_project_selection": True,
        "project_candidates": candidates,
        "pending_request": resolution.payload,
    }


def _candidate_payload(project: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(project["id"]),
        "merchant_name": str(project.get("merchant_name") or ""),
        "project_name": str(project.get("project_name") or ""),
        "city": str(project.get("city") or ""),
        "store_scope": str(project.get("store_scope") or ""),
        "start_date": project.get("start_date"),
        "end_date": project.get("end_date"),
        "project_status": str(project.get("project_status") or ""),
    }


def _candidate_label(candidate: Mapping[str, Any]) -> str:
    identity = [
        str(candidate.get("merchant_name") or "").strip(),
        str(candidate.get("project_name") or "").strip(),
    ]
    location = " ".join(
        part for part in (
            str(candidate.get("city") or "").strip(),
            str(candidate.get("store_scope") or "").strip(),
        ) if part
    )
    if location:
        identity.append(location)
    start = str(candidate.get("start_date") or "").strip()
    end = str(candidate.get("end_date") or "").strip()
    if start and end:
        identity.append(f"{start} 至 {end}")
    elif start:
        identity.append(f"{start} 开始")
    status = _STATUS_LABELS.get(
        str(candidate.get("project_status") or ""),
        str(candidate.get("project_status") or ""),
    )
    if status:
        identity.append(status)
    return "｜".join(part for part in identity if part)
