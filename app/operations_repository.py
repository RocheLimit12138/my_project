"""售中和售后业务数据的 MySQL 仓储及测试用内存仓储。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any
from uuid import uuid4

from app.database import open_database_connection
from app.operations_models import ClosureInput, ProjectCreateInput, WeeklyUpdateInput


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return deepcopy(default)
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("数据库中的业务 JSON 无法解析，请检查数据完整性。") from exc


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _plain_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _plain(value) for key, value in row.items()}


class MySQLTrackingRepository:
    """使用独立业务表保存项目、周数据、报告和合作总结。"""

    @staticmethod
    def _weekly_payload(update: WeeklyUpdateInput) -> dict[str, Any]:
        """将待审核周度输入序列化为可重放的业务载荷。"""

        return {
            "project_id": update.project_id,
            "period_start": update.period_start.isoformat(),
            "period_end": update.period_end.isoformat(),
            "metrics": [metric.to_dict() for metric in update.metrics],
            "completed_items": update.completed_items,
            "current_issues": update.current_issues,
            "pending_confirmations": update.pending_confirmations,
            "next_week_plan": update.next_week_plan,
            "risk_notes": update.risk_notes,
        }

    @staticmethod
    def _write_weekly_update_cursor(cursor: Any, update: WeeklyUpdateInput) -> int:
        """在调用方事务中写入正式周度表，供审核通过复用。"""

        cursor.execute(
            "SELECT id FROM tracking_projects WHERE id = %s FOR UPDATE",
            (update.project_id,),
        )
        if cursor.fetchone() is None:
            raise ValueError("跟踪项目不存在或已被删除。")
        cursor.execute(
            """
            SELECT id FROM project_weekly_updates
            WHERE project_id = %s AND period_start = %s AND period_end = %s
            FOR UPDATE
            """,
            (update.project_id, update.period_start, update.period_end),
        )
        existing = cursor.fetchone()
        if existing:
            update_id = int(existing["id"])
            cursor.execute(
                """
                UPDATE project_weekly_updates
                SET completed_items = %s, current_issues = %s,
                    pending_confirmations = %s, next_week_plan = %s,
                    risk_notes = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    update.completed_items or None, update.current_issues or None,
                    update.pending_confirmations or None, update.next_week_plan or None,
                    update.risk_notes or None, update_id,
                ),
            )
            cursor.execute("DELETE FROM project_weekly_metrics WHERE weekly_update_id = %s", (update_id,))
        else:
            cursor.execute(
                """
                INSERT INTO project_weekly_updates (
                    project_id, period_start, period_end, completed_items,
                    current_issues, pending_confirmations, next_week_plan, risk_notes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    update.project_id, update.period_start, update.period_end,
                    update.completed_items or None, update.current_issues or None,
                    update.pending_confirmations or None, update.next_week_plan or None,
                    update.risk_notes or None,
                ),
            )
            update_id = int(cursor.lastrowid)
        for metric in update.metrics:
            cursor.execute(
                """
                INSERT INTO project_weekly_metrics (
                    weekly_update_id, project_id, metric_name, actual_value,
                    data_source, note
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    update_id, update.project_id, metric.name, metric.actual_value,
                    metric.source, metric.note or None,
                ),
            )
        cursor.execute(
            "UPDATE tracking_projects SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (update.project_id,),
        )
        return update_id

    def create_project(self, project: ProjectCreateInput) -> int:
        project_code = "TRK-" + uuid4().hex[:12].upper()
        with open_database_connection() as connection:
            try:
                connection.begin()
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO tracking_projects (
                            project_code, project_name, merchant_name, city, industry,
                            store_scope, owner_name, project_status, start_date, end_date, notes
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
                        """,
                        (
                            project_code, project.project_name, project.merchant_name,
                            project.city or None, project.industry or None,
                            project.store_scope or None, project.owner_name or None,
                            project.start_date, project.end_date, project.notes or None,
                        ),
                    )
                    project_id = int(cursor.lastrowid)
                    cursor.execute(
                        """
                        INSERT INTO project_proposal_snapshots (
                            project_id, version_no, source_type, snapshot_json, is_current
                        ) VALUES (%s, 1, %s, %s, 1)
                        """,
                        (project_id, project.proposal_source, _json_dump(project.proposal_snapshot)),
                    )
                    for index, metric in enumerate(project.metrics, start=1):
                        cursor.execute(
                            """
                            INSERT INTO project_metric_definitions (
                                project_id, metric_name, unit, baseline_value, target_value,
                                target_direction, aggregation_method, data_definition, sort_order
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                project_id, metric.name, metric.unit or None,
                                metric.baseline_value, metric.target_value, metric.direction,
                                metric.aggregation, metric.data_definition or None, index,
                            ),
                        )
                connection.commit()
                return project_id
            except Exception:
                connection.rollback()
                raise

    def list_projects(self) -> list[dict[str, Any]]:
        with open_database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, project_code, project_name, merchant_name, city, industry,
                       store_scope, project_status, start_date, end_date, owner_name
                FROM tracking_projects
                ORDER BY updated_at DESC, id DESC
                """
            )
            return [_plain_row(row) for row in cursor.fetchall()]

    def get_project(self, project_id: int) -> dict[str, Any]:
        with open_database_connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM tracking_projects WHERE id = %s", (project_id,))
            project = cursor.fetchone()
            if project is None:
                raise ValueError("跟踪项目不存在或已被删除。")
            cursor.execute(
                """
                SELECT id, version_no, source_type, snapshot_json, created_at
                FROM project_proposal_snapshots
                WHERE project_id = %s AND is_current = 1
                ORDER BY version_no DESC LIMIT 1
                """,
                (project_id,),
            )
            snapshot = cursor.fetchone()
            cursor.execute(
                """
                SELECT id, metric_name, unit, baseline_value, target_value,
                       target_direction, aggregation_method, data_definition, sort_order
                FROM project_metric_definitions
                WHERE project_id = %s ORDER BY sort_order, id
                """,
                (project_id,),
            )
            metrics = [_plain_row(row) for row in cursor.fetchall()]
        result = _plain_row(project)
        result["proposal_snapshot"] = _json_load(snapshot["snapshot_json"], {}) if snapshot else {}
        result["proposal_source"] = snapshot["source_type"] if snapshot else None
        result["proposal_version"] = snapshot["version_no"] if snapshot else None
        result["metrics"] = metrics
        return result

    def save_weekly_update(self, update: WeeklyUpdateInput) -> int:
        with open_database_connection() as connection:
            try:
                connection.begin()
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM tracking_projects WHERE id = %s FOR UPDATE",
                        (update.project_id,),
                    )
                    if cursor.fetchone() is None:
                        raise ValueError("跟踪项目不存在或已被删除。")
                    cursor.execute(
                        """
                        SELECT id FROM project_weekly_updates
                        WHERE project_id = %s AND period_start = %s AND period_end = %s
                        FOR UPDATE
                        """,
                        (update.project_id, update.period_start, update.period_end),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        update_id = int(existing["id"])
                        cursor.execute(
                            """
                            UPDATE project_weekly_updates
                            SET completed_items = %s, current_issues = %s,
                                pending_confirmations = %s, next_week_plan = %s,
                                risk_notes = %s, updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """,
                            (
                                update.completed_items or None, update.current_issues or None,
                                update.pending_confirmations or None, update.next_week_plan or None,
                                update.risk_notes or None, update_id,
                            ),
                        )
                        cursor.execute("DELETE FROM project_weekly_metrics WHERE weekly_update_id = %s", (update_id,))
                    else:
                        cursor.execute(
                            """
                            INSERT INTO project_weekly_updates (
                                project_id, period_start, period_end, completed_items,
                                current_issues, pending_confirmations, next_week_plan, risk_notes
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                update.project_id, update.period_start, update.period_end,
                                update.completed_items or None, update.current_issues or None,
                                update.pending_confirmations or None, update.next_week_plan or None,
                                update.risk_notes or None,
                            ),
                        )
                        update_id = int(cursor.lastrowid)
                    for metric in update.metrics:
                        cursor.execute(
                            """
                            INSERT INTO project_weekly_metrics (
                                weekly_update_id, project_id, metric_name, actual_value,
                                data_source, note
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (
                                update_id, update.project_id, metric.name, metric.actual_value,
                                metric.source, metric.note or None,
                            ),
                        )
                    cursor.execute(
                        "UPDATE tracking_projects SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (update.project_id,),
                    )
                connection.commit()
                return update_id
            except Exception:
                connection.rollback()
                raise

    def submit_weekly_update(self, update: WeeklyUpdateInput, *, submitted_by: str) -> int:
        """把销售输入写入待审核表，不触及正式周度记录。"""

        if not submitted_by.strip():
            raise ValueError("提交人不能为空。")
        payload = self._weekly_payload(update)
        with open_database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO project_weekly_submissions (
                    project_id, period_start, period_end, payload_json, submitted_by
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    update.project_id, update.period_start, update.period_end,
                    _json_dump(payload), submitted_by.strip(),
                ),
            )
            return int(cursor.lastrowid)

    def list_pending_weekly(self) -> list[dict[str, Any]]:
        """返回待审核周度输入及可读项目资料。"""

        with open_database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT submissions.id, submissions.project_id, submissions.period_start,
                       submissions.period_end, submissions.payload_json, submissions.submitted_by,
                       submissions.review_status, submissions.review_note, submissions.created_at,
                       projects.project_name, projects.merchant_name, projects.city, projects.store_scope
                FROM project_weekly_submissions AS submissions
                INNER JOIN tracking_projects AS projects ON projects.id = submissions.project_id
                WHERE submissions.review_status = 'pending'
                ORDER BY submissions.created_at, submissions.id
                """
            )
            result = []
            for row in cursor.fetchall():
                item = _plain_row(row)
                item["payload"] = _json_load(item.pop("payload_json"), {})
                result.append(item)
            return result

    def review_weekly_submission(
        self,
        submission_id: int,
        *,
        decision: str,
        reviewer: str,
        note: str = "",
    ) -> dict[str, Any]:
        """审核待提交记录；通过时同一事务写入正式周度表。"""

        if decision not in {"approve", "reject"}:
            raise ValueError("审核决定必须是 approve 或 reject。")
        if not reviewer.strip():
            raise ValueError("审核人不能为空。")
        with open_database_connection() as connection:
            try:
                connection.begin()
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT * FROM project_weekly_submissions
                        WHERE id = %s FOR UPDATE
                        """,
                        (submission_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise ValueError("待审核的周度提交不存在。")
                    if row["review_status"] != "pending":
                        raise ValueError("该周度提交已经审核过。")
                    payload = _json_load(row["payload_json"], {})
                    update = WeeklyUpdateInput.from_mapping(
                        payload,
                        metrics=payload.get("metrics") or (),
                    )
                    update_id = None
                    if decision == "approve":
                        update_id = self._write_weekly_update_cursor(cursor, update)
                    cursor.execute(
                        """
                        UPDATE project_weekly_submissions
                        SET review_status = %s, review_note = %s,
                            reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (
                            "approved" if decision == "approve" else "rejected",
                            note.strip() or None, reviewer.strip(), submission_id,
                        ),
                    )
                connection.commit()
                return {
                    "submission_id": submission_id,
                    "review_status": "approved" if decision == "approve" else "rejected",
                    "weekly_update_id": update_id,
                    "reviewed_by": reviewer.strip(),
                    "review_note": note.strip(),
                }
            except Exception:
                connection.rollback()
                raise

    def list_weekly_updates(self, project_id: int) -> list[dict[str, Any]]:
        with open_database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM project_weekly_updates
                WHERE project_id = %s ORDER BY period_start, period_end, id
                """,
                (project_id,),
            )
            updates = [_plain_row(row) for row in cursor.fetchall()]
            if not updates:
                return []
            update_ids = [item["id"] for item in updates]
            placeholders = ",".join(["%s"] * len(update_ids))
            cursor.execute(
                f"""
                SELECT weekly_update_id, metric_name, actual_value, data_source, note
                FROM project_weekly_metrics
                WHERE weekly_update_id IN ({placeholders})
                ORDER BY id
                """,
                tuple(update_ids),
            )
            grouped: dict[int, list[dict[str, Any]]] = {int(item): [] for item in update_ids}
            for row in cursor.fetchall():
                metric = _plain_row(row)
                grouped[int(metric.pop("weekly_update_id"))].append(metric)
            for update in updates:
                update["metrics"] = grouped[int(update["id"])]
            return updates

    def save_weekly_report(
        self,
        *,
        project_id: int,
        weekly_update_id: int,
        report_markdown: str,
        analysis: list[dict[str, Any]],
    ) -> int:
        with open_database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO project_weekly_reports (
                    project_id, weekly_update_id, report_markdown,
                    analysis_json, review_status
                ) VALUES (%s, %s, %s, %s, 'requires_business_review')
                ON DUPLICATE KEY UPDATE
                    report_markdown = VALUES(report_markdown),
                    analysis_json = VALUES(analysis_json),
                    review_status = 'requires_business_review',
                    generated_at = CURRENT_TIMESTAMP
                """,
                (project_id, weekly_update_id, report_markdown, _json_dump(analysis)),
            )
            return int(cursor.lastrowid or weekly_update_id)

    def list_weekly_reports(self, project_id: int) -> list[dict[str, Any]]:
        with open_database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT reports.id, reports.weekly_update_id, updates.period_start,
                       updates.period_end, reports.report_markdown,
                       reports.analysis_json, reports.review_status, reports.generated_at
                FROM project_weekly_reports AS reports
                INNER JOIN project_weekly_updates AS updates
                    ON updates.id = reports.weekly_update_id
                WHERE reports.project_id = %s
                ORDER BY updates.period_start, reports.id
                """,
                (project_id,),
            )
            reports = []
            for row in cursor.fetchall():
                item = _plain_row(row)
                item["analysis"] = _json_load(item.pop("analysis_json"), [])
                reports.append(item)
            return reports

    def get_closure_report(self, project_id: int) -> dict[str, Any] | None:
        with open_database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, project_id, proposal_snapshot_id, closure_date, final_status,
                       completed_items, deliverables, unresolved_items, customer_feedback,
                       continuation_recommendation, next_direction, limitations,
                       comparison_json, report_markdown, review_status, generated_at
                FROM project_closure_reports WHERE project_id = %s
                """,
                (project_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            result = _plain_row(row)
            result["comparison"] = _json_load(result.pop("comparison_json"), [])
            return result

    def save_closure_report(
        self,
        closure: ClosureInput,
        *,
        report_markdown: str,
        comparison: list[dict[str, Any]],
    ) -> int:
        with open_database_connection() as connection:
            try:
                connection.begin()
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM tracking_projects WHERE id = %s FOR UPDATE",
                        (closure.project_id,),
                    )
                    if cursor.fetchone() is None:
                        raise ValueError("跟踪项目不存在或已被删除。")
                    cursor.execute("DELETE FROM project_final_metrics WHERE project_id = %s", (closure.project_id,))
                    for metric in closure.final_metrics:
                        cursor.execute(
                            """
                            INSERT INTO project_final_metrics (
                                project_id, metric_name, final_value, data_source, note
                            ) VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                closure.project_id, metric.name, metric.actual_value,
                                metric.source, metric.note or None,
                            ),
                        )
                    cursor.execute(
                        """
                        SELECT id FROM project_proposal_snapshots
                        WHERE project_id = %s AND is_current = 1
                        ORDER BY version_no DESC LIMIT 1
                        """,
                        (closure.project_id,),
                    )
                    snapshot = cursor.fetchone()
                    snapshot_id = int(snapshot["id"]) if snapshot else None
                    cursor.execute(
                        """
                        INSERT INTO project_closure_reports (
                            project_id, proposal_snapshot_id, closure_date, final_status, completed_items,
                            deliverables, unresolved_items, customer_feedback,
                            continuation_recommendation, next_direction, limitations,
                            comparison_json, report_markdown, review_status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                  'requires_business_review')
                        ON DUPLICATE KEY UPDATE
                            proposal_snapshot_id = VALUES(proposal_snapshot_id),
                            closure_date = VALUES(closure_date),
                            final_status = VALUES(final_status),
                            completed_items = VALUES(completed_items),
                            deliverables = VALUES(deliverables),
                            unresolved_items = VALUES(unresolved_items),
                            customer_feedback = VALUES(customer_feedback),
                            continuation_recommendation = VALUES(continuation_recommendation),
                            next_direction = VALUES(next_direction),
                            limitations = VALUES(limitations),
                            comparison_json = VALUES(comparison_json),
                            report_markdown = VALUES(report_markdown),
                            review_status = 'requires_business_review',
                            generated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            closure.project_id, snapshot_id, closure.closure_date, closure.final_status,
                            closure.completed_items or None, closure.deliverables or None,
                            closure.unresolved_items or None, closure.customer_feedback or None,
                            closure.continuation_recommendation or None,
                            closure.next_direction or None, closure.limitations or None,
                            _json_dump(comparison), report_markdown,
                        ),
                    )
                    report_id = int(cursor.lastrowid or closure.project_id)
                    project_status = "cancelled" if closure.final_status == "cancelled" else "completed"
                    cursor.execute(
                        """
                        UPDATE tracking_projects
                        SET project_status = %s, end_date = COALESCE(end_date, %s),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (project_status, closure.closure_date, closure.project_id),
                    )
                connection.commit()
                return report_id
            except Exception:
                connection.rollback()
                raise


class InMemoryTrackingRepository:
    """无外部写入的确定性仓储，用于单元测试和业务规则演示。"""

    def __init__(self) -> None:
        self.projects: dict[int, dict[str, Any]] = {}
        self.updates: dict[int, list[dict[str, Any]]] = {}
        self.weekly_submissions: list[dict[str, Any]] = []
        self.weekly_reports: list[dict[str, Any]] = []
        self.closure_reports: list[dict[str, Any]] = []
        self._project_id = 0
        self._update_id = 0

    def create_project(self, project: ProjectCreateInput) -> int:
        self._project_id += 1
        project_id = self._project_id
        self.projects[project_id] = {
            "id": project_id,
            "project_code": f"TEST-{project_id:04d}",
            "project_name": project.project_name,
            "merchant_name": project.merchant_name,
            "city": project.city,
            "industry": project.industry,
            "store_scope": project.store_scope,
            "owner_name": project.owner_name,
            "project_status": "active",
            "start_date": project.start_date.isoformat() if project.start_date else None,
            "end_date": project.end_date.isoformat() if project.end_date else None,
            "notes": project.notes,
            "proposal_snapshot": deepcopy(project.proposal_snapshot),
            "proposal_source": project.proposal_source,
            "proposal_version": 1,
            "metrics": [
                {
                    "id": index,
                    "metric_name": metric.name,
                    "unit": metric.unit,
                    "baseline_value": str(metric.baseline_value) if metric.baseline_value is not None else None,
                    "target_value": str(metric.target_value) if metric.target_value is not None else None,
                    "target_direction": metric.direction,
                    "aggregation_method": metric.aggregation,
                    "data_definition": metric.data_definition,
                    "sort_order": index,
                }
                for index, metric in enumerate(project.metrics, start=1)
            ],
        }
        self.updates[project_id] = []
        return project_id

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            {key: deepcopy(value) for key, value in project.items() if key in {
                "id", "project_code", "project_name", "merchant_name", "project_status",
                "city", "industry", "store_scope", "start_date", "end_date", "owner_name",
            }}
            for project in reversed(self.projects.values())
        ]

    def get_project(self, project_id: int) -> dict[str, Any]:
        if project_id not in self.projects:
            raise ValueError("跟踪项目不存在或已被删除。")
        return deepcopy(self.projects[project_id])

    def save_weekly_update(self, update: WeeklyUpdateInput) -> int:
        if update.project_id not in self.projects:
            raise ValueError("跟踪项目不存在或已被删除。")
        rows = self.updates[update.project_id]
        existing = next((row for row in rows if row["period_start"] == update.period_start.isoformat()
                         and row["period_end"] == update.period_end.isoformat()), None)
        if existing is None:
            self._update_id += 1
            existing = {"id": self._update_id, "project_id": update.project_id}
            rows.append(existing)
        existing.update({
            "period_start": update.period_start.isoformat(),
            "period_end": update.period_end.isoformat(),
            "completed_items": update.completed_items,
            "current_issues": update.current_issues,
            "pending_confirmations": update.pending_confirmations,
            "next_week_plan": update.next_week_plan,
            "risk_notes": update.risk_notes,
            "metrics": [
                {
                    "metric_name": metric.name,
                    "actual_value": str(metric.actual_value),
                    "data_source": metric.source,
                    "note": metric.note,
                }
                for metric in update.metrics
            ],
        })
        rows.sort(key=lambda item: (item["period_start"], item["period_end"], item["id"]))
        return int(existing["id"])

    def submit_weekly_update(self, update: WeeklyUpdateInput, *, submitted_by: str) -> int:
        if update.project_id not in self.projects:
            raise ValueError("跟踪项目不存在或已被删除。")
        if not submitted_by.strip():
            raise ValueError("提交人不能为空。")
        submission_id = len(self.weekly_submissions) + 1
        self.weekly_submissions.append({
            "id": submission_id,
            "project_id": update.project_id,
            "period_start": update.period_start.isoformat(),
            "period_end": update.period_end.isoformat(),
            "payload": {
                "project_id": update.project_id,
                "period_start": update.period_start.isoformat(),
                "period_end": update.period_end.isoformat(),
                "metrics": [metric.to_dict() for metric in update.metrics],
                "completed_items": update.completed_items,
                "current_issues": update.current_issues,
                "pending_confirmations": update.pending_confirmations,
                "next_week_plan": update.next_week_plan,
                "risk_notes": update.risk_notes,
            },
            "submitted_by": submitted_by.strip(),
            "review_status": "pending",
            "review_note": "",
            "reviewed_by": None,
        })
        return submission_id

    def list_pending_weekly(self) -> list[dict[str, Any]]:
        return deepcopy([
            item for item in self.weekly_submissions if item["review_status"] == "pending"
        ])

    def review_weekly_submission(
        self,
        submission_id: int,
        *,
        decision: str,
        reviewer: str,
        note: str = "",
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ValueError("审核决定必须是 approve 或 reject。")
        item = next((row for row in self.weekly_submissions if row["id"] == submission_id), None)
        if item is None:
            raise ValueError("待审核的周度提交不存在。")
        if item["review_status"] != "pending":
            raise ValueError("该周度提交已经审核过。")
        update_id = None
        if decision == "approve":
            payload = item["payload"]
            update = WeeklyUpdateInput.from_mapping(payload, metrics=payload.get("metrics") or ())
            update_id = self.save_weekly_update(update)
        item["review_status"] = "approved" if decision == "approve" else "rejected"
        item["reviewed_by"] = reviewer
        item["review_note"] = note.strip()
        return {
            "submission_id": submission_id,
            "review_status": item["review_status"],
            "weekly_update_id": update_id,
            "reviewed_by": reviewer,
            "review_note": note.strip(),
        }

    def list_weekly_updates(self, project_id: int) -> list[dict[str, Any]]:
        if project_id not in self.projects:
            raise ValueError("跟踪项目不存在或已被删除。")
        return deepcopy(self.updates[project_id])

    def save_weekly_report(self, **value: Any) -> int:
        self.weekly_reports.append(deepcopy(value))
        return len(self.weekly_reports)

    def list_weekly_reports(self, project_id: int) -> list[dict[str, Any]]:
        return deepcopy([
            item for item in self.weekly_reports if int(item["project_id"]) == project_id
        ])

    def get_closure_report(self, project_id: int) -> dict[str, Any] | None:
        for index, item in enumerate(reversed(self.closure_reports), start=1):
            if int(item["closure"]["project_id"]) == project_id:
                result = deepcopy(item)
                result["id"] = len(self.closure_reports) - index + 1
                return result
        return None

    def save_closure_report(
        self,
        closure: ClosureInput,
        *,
        report_markdown: str,
        comparison: list[dict[str, Any]],
    ) -> int:
        if closure.project_id not in self.projects:
            raise ValueError("跟踪项目不存在或已被删除。")
        self.projects[closure.project_id]["project_status"] = (
            "cancelled" if closure.final_status == "cancelled" else "completed"
        )
        self.closure_reports.append({
            "closure": asdict(closure),
            "report_markdown": report_markdown,
            "comparison": deepcopy(comparison),
        })
        return len(self.closure_reports)
