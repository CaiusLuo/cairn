"""Start an isolated Server and exercise the shipped bundle in Playwright."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from tests.support.git import initialize_repository
from tests.support.pg import database_for
from tests.support.server_process import ROOT, NativeClient, ServerProcess, wait_for

REVIEW_TIMEZONE = "Asia/Shanghai"
REVIEW_ACCOUNT = "browser-fixture-bot"
REVIEW_RECIPIENT = "browser-fixture-sender"


def _seed_reviews(server: ServerProcess) -> None:
    """Write review fixtures through the real store so the Web console reads them."""
    import hashlib
    from datetime import date, datetime, time, timedelta
    from zoneinfo import ZoneInfo

    from cairn.knowledge.store import KnowledgeStore
    from cairn.review.models import (
        DailyReview,
        DeliveryTarget,
        ReviewInput,
        ReviewItem,
        ReviewType,
        SourceCollectionState,
        SourceCollectionStatus,
        SourceSnapshot,
        SourceTrust,
        WeeklyReview,
    )
    from cairn.review.store import ReviewStore
    from cairn.review.weekly import week_period

    zone = ZoneInfo(REVIEW_TIMEZONE)
    store = ReviewStore(KnowledgeStore(database_for(server.root)))

    def source(source_id: str, text: str, occurred_at: datetime) -> SourceSnapshot:
        return SourceSnapshot(
            source_type="clip",
            source_id=source_id,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            occurred_at=occurred_at.isoformat(),
            trust=SourceTrust.USER_RECORDED,
            may_prove_completion=False,
            verification="clip_saved",
            snapshot=text,
        )

    def review_input(
        review_type: ReviewType,
        day: date,
        start: datetime,
        end: datetime,
        sources: tuple[SourceSnapshot, ...],
    ) -> ReviewInput:
        return ReviewInput.build(
            review_type=review_type,
            day=day,
            timezone=REVIEW_TIMEZONE,
            period_start=start,
            period_end=end,
            sources=sources,
            collection_status=(
                SourceCollectionStatus(
                    source_type="clip",
                    status=SourceCollectionState.OK,
                    discovered=len(sources),
                    included=len(sources),
                ),
            ),
            budget_chars=60_000,
        )

    def persist(
        value: ReviewInput, request_id: str, *, regenerate: bool = False
    ) -> str:
        return store.create_or_get(
            request_id=request_id,
            review_type=value.review_type,
            day=value.date,
            period_start=value.period_start.isoformat(),
            period_end=value.period_end.isoformat(),
            timezone=value.timezone,
            input_fingerprint=value.input_fingerprint,
            source_manifest=tuple(item.manifest_entry() for item in value.sources),
            collection_status=value.collection_status,
            source_snapshots=value.included_sources,
            regenerate=regenerate,
        ).review.review_id

    def completed(
        value: ReviewInput,
        request_id: str,
        summary: DailyReview | WeeklyReview,
        markdown: str,
        *,
        regenerate: bool = False,
    ) -> str:
        review_id = persist(value, request_id, regenerate=regenerate)
        store.mark_generating(review_id, "fixture-review-model")
        store.complete(
            review_id,
            summary=summary,
            summary_markdown=markdown,
            model_id="fixture-review-model",
        )
        return review_id

    day = date(2026, 1, 15)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    daily_sources = (
        source(
            "clip-review-source",
            '{"clip":"日报来源"}\n\n保留了可核对的知识片段。',
            start + timedelta(hours=9),
        ),
        source(
            "task-review-source",
            '{"task_id":"task-review","status":"completed"}',
            start + timedelta(hours=15),
        ),
    )
    daily_input = review_input(ReviewType.DAILY, day, start, end, daily_sources)
    completed(
        daily_input,
        "e2e-daily-v1",
        DailyReview(
            date=day,
            overview="第一版日报: 共享运行与知识片段的验收通过。",
            completed_activities=(
                ReviewItem(
                    text="完成共享运行验收。",
                    source_refs=("task-review-source",),
                ),
            ),
            knowledge_insights=(
                ReviewItem(
                    text="复盘证据保留在知识库。",
                    source_refs=("clip-review-source",),
                ),
            ),
            source_refs=("clip-review-source", "task-review-source"),
            confidence_notes=("离线验收数据。",),
        ),
        "# 2026-01-15 日报\n\n## 概览\n\n第一版日报: 共享运行与知识片段的验收通过。\n",
    )
    second_daily = completed(
        daily_input,
        "e2e-daily-v2",
        DailyReview(
            date=day,
            overview="第二版日报: 补充了复盘投递的状态核对。",
            completed_activities=(
                ReviewItem(
                    text="完成共享运行验收。",
                    source_refs=("task-review-source",),
                ),
            ),
            knowledge_insights=(
                ReviewItem(
                    text="复盘证据保留在知识库。",
                    source_refs=("clip-review-source",),
                ),
            ),
            recommendations=(
                ReviewItem(
                    text="继续核对微信投递的真实状态。",
                    source_refs=("clip-review-source",),
                ),
            ),
            source_refs=("clip-review-source", "task-review-source"),
            confidence_notes=("第二版补充了投递核对。",),
        ),
        "# 2026-01-15 日报\n\n## 概览\n\n第二版日报: 补充了复盘投递的状态核对。\n",
        regenerate=True,
    )

    failed_day = date(2026, 1, 16)
    failed_start = datetime.combine(failed_day, time.min, tzinfo=zone)
    failed_id = persist(
        review_input(
            ReviewType.DAILY,
            failed_day,
            failed_start,
            failed_start + timedelta(days=1),
            (daily_sources[0],),
        ),
        "e2e-daily-failed",
    )
    store.fail(failed_id, "provider_unavailable")

    period = week_period("2026-W03", timezone=REVIEW_TIMEZONE)
    weekly_input = review_input(
        ReviewType.WEEKLY,
        period.day,
        period.period_start,
        period.period_end,
        (
            source(
                "clip-weekly-source",
                '{"clip":"周报来源"}\n\n本周复盘的证据片段。',
                period.period_start + timedelta(days=2),
            ),
        ),
    )
    weekly_id = completed(
        weekly_input,
        "e2e-weekly-v1",
        WeeklyReview(
            week_id=period.week_id,
            period_start=period.period_start,
            period_end=period.period_end,
            overview="第 3 周复盘: 完成周报与人工重试链路。",
            major_events=(
                ReviewItem(
                    text="完成周报生成与 Web 查看。",
                    source_refs=("clip-weekly-source",),
                ),
            ),
            progress=(
                ReviewItem(
                    text="复盘证据可以追溯到原始片段。",
                    source_refs=("clip-weekly-source",),
                ),
            ),
            next_week_priorities=(
                ReviewItem(
                    text="核对微信投递结果。",
                    source_refs=("clip-weekly-source",),
                ),
            ),
            source_refs=("clip-weekly-source",),
            confidence_notes=(),
        ),
        "# 2026-W03 周报\n\n## 概览\n\n第 3 周复盘: 完成周报与人工重试链路。\n",
    )

    target = DeliveryTarget(account_id=REVIEW_ACCOUNT, recipient=REVIEW_RECIPIENT)

    def delivery(review_id: str, outcome: str) -> None:
        store.ensure_delivery(review_id, target=target)
        attempt = store.begin_delivery(
            review_id,
            account_id=REVIEW_ACCOUNT,
            sender_id=REVIEW_RECIPIENT,
            context_token="fixture-context",
        )
        assert attempt.allowed, attempt.reason
        if outcome == "accepted":
            store.mark_delivery_accepted(review_id)
        else:
            store.mark_delivery_unknown(review_id, "send_unconfirmed")

    delivery(second_daily, "accepted")
    delivery(weekly_id, "delivery_unknown")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="Playwright browser channel, e.g. chrome")
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="cairn-workbench-") as directory:
        root = Path(directory).resolve()
        source = root / "source"
        source.mkdir()
        initialize_repository(source)
        server = ServerProcess(
            source,
            root,
            extra_config=(
                "[review]\n"
                "enabled = true\n"
                "weekly_enabled = true\n"
                'time = "20:30"\n'
                'weekly_time = "20:40"\n'
                f'timezone = "{REVIEW_TIMEZONE}"\n'
                f'account_id = "{REVIEW_ACCOUNT}"\n'
                f'recipient = "{REVIEW_RECIPIENT}"\n'
            ),
        )
        try:
            server.start()
            token = server.state / "runtime.token"
            headers = {"Authorization": "Bearer " + token.read_text().strip()}
            with httpx.Client(
                base_url=server.url, headers=headers, trust_env=False, timeout=5
            ) as api:
                terminal = NativeClient(source, server.url, token)
                try:
                    wait_for(lambda: b"Shared Runtime" in terminal.output)
                    terminal.send("CLI browser takeover")

                    def cli_settled() -> bool:
                        return any(
                            item["initial_prompt"] == "CLI browser takeover"
                            and (item["latest_run"] or {}).get("status") == "completed"
                            for item in api.get("/api/runtime/conversations").json()
                        )

                    wait_for(cli_settled)
                    terminal.send("/exit")
                    terminal.process.wait(timeout=10)
                finally:
                    terminal.close()

                legacy = (
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": "Legacy browser history",
                            "source": "cli",
                            "request_id": "legacy-browser",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                api.post(
                    f"/api/runtime/conversations/{legacy['conversation_id']}/messages",
                    json={
                        "prompt": "Legacy browser history",
                        "source": "cli",
                        "request_id": "legacy-browser-message",
                    },
                ).raise_for_status()

                def legacy_settled() -> bool:
                    return any(
                        item["conversation_id"] == legacy["conversation_id"]
                        and (item["latest_run"] or {}).get("status") == "completed"
                        for item in api.get("/api/runtime/conversations").json()
                    )

                wait_for(legacy_settled)
                missing = (
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": "Missing session continuation",
                            "source": "cli",
                            "request_id": "missing-session",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                api.post(
                    f"/api/runtime/conversations/{missing['conversation_id']}/messages",
                    json={
                        "prompt": "Missing session continuation",
                        "source": "cli",
                        "request_id": "missing-session-message",
                    },
                ).raise_for_status()

                def missing_settled() -> bool:
                    return any(
                        item["conversation_id"] == missing["conversation_id"]
                        and (item["latest_run"] or {}).get("status") == "completed"
                        for item in api.get("/api/runtime/conversations").json()
                    )

                wait_for(missing_settled)
                # Enough conversations to exercise the latest-first limit and
                # the expand control in the conversation tree.
                for index in range(13):
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": f"Pagination fixture {index}",
                            "source": "cli",
                            "request_id": f"pagination-fixture-{index}",
                        },
                    ).raise_for_status()
                long_title = "Long conversation title " + "超长标题" * 40 + " END"
                long_conversation = (
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": long_title,
                            "source": "cli",
                            "request_id": "long-title",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                api.post(
                    f"/api/runtime/conversations/{long_conversation['conversation_id']}/messages",
                    json={
                        "prompt": long_title,
                        "source": "cli",
                        "request_id": "long-title-message",
                    },
                ).raise_for_status()

                def long_settled() -> bool:
                    return any(
                        item["conversation_id"] == long_conversation["conversation_id"]
                        and (item["latest_run"] or {}).get("status") == "completed"
                        for item in api.get("/api/runtime/conversations").json()
                    )

                wait_for(long_settled)
                rename_conversation = (
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": "Rename delete fixture",
                            "source": "cli",
                            "request_id": "rename-delete-fixture",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                # Reproduce the deployed shapes: old Runs predate Session
                # identity, and a newer Run points at a Session row that no
                # longer exists.
                server.stop()
                with database_for(server.root).connect("core") as database:
                    database.execute(
                        "UPDATE runs SET session_id = NULL WHERE conversation_id = %s",
                        (legacy["conversation_id"],),
                    )
                    database.execute(
                        "UPDATE runs SET session_id = %s WHERE conversation_id = %s",
                        (
                            "00000000-0000-4000-8000-000000000000",
                            missing["conversation_id"],
                        ),
                    )
                server.start()

                gate = server.gates / "after"
                gate.touch()
                task = (
                    api.post(
                        "/api/runtime/tasks",
                        json={
                            "prompt": "Interrupted browser recovery",
                            "source": "cli",
                            "mode": "development",
                            "request_id": "browser-recovery",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                wait_for(lambda: gate.with_suffix(".entered").exists())
                path = "/api/runtime/tasks/" + task["task_id"]
                running = api.get(path).raise_for_status().json()
                artifact = Path(running["worktree"]) / "runtime-result.txt"
                original = artifact.stat()
                server.stop(crash=True)
                gate.unlink()
                server.start()
                assert (
                    api.get(path).raise_for_status().json()["status"]
                    == "recovery_required"
                )
                _seed_reviews(server)
                environment = {
                    **os.environ,
                    "CAIRN_WEB_E2E_URL": server.url,
                    "CAIRN_WEB_E2E_TOKEN_FILE": str(token),
                    "CAIRN_WEB_E2E_LEGACY_CONVERSATION": legacy["conversation_id"],
                    "CAIRN_WEB_E2E_MISSING_SESSION_CONVERSATION": missing[
                        "conversation_id"
                    ],
                    "CAIRN_WEB_E2E_LONG_TITLE_CONVERSATION": long_conversation[
                        "conversation_id"
                    ],
                    "CAIRN_WEB_E2E_RENAME_CONVERSATION": rename_conversation[
                        "conversation_id"
                    ],
                    "CAIRN_WEB_E2E_RECOVERY_TASK": task["task_id"],
                    "CAIRN_WEB_E2E_GATES": str(server.gates),
                    "CAIRN_WEB_E2E_REAL_MODEL": "0",
                }
                if args.channel:
                    environment["CAIRN_WEB_E2E_CHANNEL"] = args.channel
                if args.screenshots:
                    environment["CAIRN_WEB_E2E_SCREENSHOTS"] = str(
                        args.screenshots.resolve()
                    )
                subprocess.run(
                    ["pnpm", "--dir", "web", "test:e2e"],
                    cwd=ROOT,
                    env=environment,
                    check=True,
                    timeout=170,
                )
                server.stop()
                server.start()
                current = api.get(path).raise_for_status().json()
                assert current["status"] == "paused"
                assert (
                    current["recovery_note"] == "Reviewed the existing test artifact."
                )
                final = artifact.stat()
                assert (final.st_ino, final.st_mtime_ns) == (
                    original.st_ino,
                    original.st_mtime_ns,
                )
                clips = (
                    api.get("/api/knowledge/clips", params={"query": "追加行"})
                    .raise_for_status()
                    .json()["clips"]
                )
                assert len(clips) == 1
                assert clips[0]["content"].endswith("追加行")
                assert clips[0]["archived_at"] is None
                print("Recovery acknowledgement survived restart without replay.")
                print("Knowledge clip survived Server restart.")
        finally:
            server.stop()


if __name__ == "__main__":
    main()
