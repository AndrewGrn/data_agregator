from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import ParseJob, ParserAccount, Target


@dataclass(slots=True)
class JobSpec:
    parser_type: str
    target_id: int
    account_id: int | None
    job_key: str | None
    payload: dict
    priority: int = 100
    queue: str | None = None
    max_attempts: int | None = None
    run_after: dt.datetime | None = None


@dataclass(slots=True)
class FileRef:
    """Attachment reference. sha256 is set when the file is already in S3."""

    source_ref: str
    filename: str | None = None
    mime: str | None = None
    size: int | None = None
    sha256: str | None = None


@dataclass(slots=True)
class ParsedEvent:
    external_id: str | None
    observed_at: dt.datetime | None
    payload: dict
    text: str | None = None
    author_id: str | None = None
    author_label: str | None = None
    event_kind: str = "message"
    thread_id: str | None = None
    reply_to: str | None = None
    files: list[FileRef] = field(default_factory=list)


class DeferJob(Exception):
    """Ask the worker to re-run this job later without counting a failure.

    Raised by a plugin when the work is not possible *right now* for a
    reason that is not the job's fault: join pacing, a FloodWait on the
    chosen account, no free account in the pool yet.
    """

    def __init__(self, seconds: int, reason: str) -> None:
        super().__init__(reason)
        self.seconds = max(int(seconds), 1)
        self.reason = reason


class ParserPlugin:
    parser_type: str

    def generate_jobs(self, session: Session, target: Target) -> list[JobSpec]:
        raise NotImplementedError

    def run(self, session: Session, job: ParseJob, target: Target, account: ParserAccount | None) -> list[ParsedEvent]:
        raise NotImplementedError

    def sync_memberships(self, session: Session) -> dict:
        return {"checked": 0, "linked": 0}
