from __future__ import annotations

from dataclasses import dataclass

from app.db.repositories import Repository


@dataclass(slots=True, frozen=True)
class HealthReport:
    connectivity: str
    account: str
    session: str
    queue_counts: dict[str, int]
    last_successful_send: str | None
    pause_reason: str | None


async def get_health(repository: Repository) -> HealthReport:
    account = await repository.get_account()
    connectivity = await repository.get_state("connectivity") or "DISCONNECTED"
    pause_reason = await repository.get_state("outgoing_pause_reason")
    if account is None:
        account_label = "not authenticated"
        session = "UNKNOWN"
    else:
        account_label = f"@{account.username}" if account.username else account.display_name
        session = "AUTH_REQUIRED" if connectivity == "AUTH_REQUIRED" else "AUTHORIZED (last known)"
    return HealthReport(
        connectivity=connectivity,
        account=account_label,
        session=session,
        queue_counts=await repository.queue_counts(),
        last_successful_send=await repository.last_successful_send(),
        pause_reason=pause_reason,
    )


def format_health(report: HealthReport) -> str:
    counts = report.queue_counts
    pending = counts.get("PENDING", 0)
    scheduled = counts.get("SCHEDULED", 0)
    retrying = counts.get("RETRY", 0) + counts.get("WAITING_RATE_LIMIT", 0)
    failed = counts.get("FAILED", 0)
    review = counts.get("REVIEW_REQUIRED", 0)
    lines = [
        "Application: OK",
        f"Telegram: {report.connectivity}",
        f"Account: {report.account}",
        f"Session: {report.session}",
        "Database: OK",
        "",
        "Queue:",
        f"  pending: {pending}",
        f"  scheduled: {scheduled}",
        f"  retrying: {retrying}",
        f"  failed: {failed}",
        f"  review required: {review}",
        "",
        f"Last successful send: {report.last_successful_send or 'never'}",
    ]
    if report.pause_reason:
        lines.extend(("", f"Outgoing sends paused: {report.pause_reason}"))
    return "\n".join(lines)
