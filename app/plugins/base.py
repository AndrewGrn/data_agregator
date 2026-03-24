from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

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


@dataclass(slots=True)
class ParsedEvent:
    external_id: str | None
    observed_at: dt.datetime | None
    payload: dict


class ParserPlugin:
    parser_type: str

    def generate_jobs(self, session: Session, target: Target) -> list[JobSpec]:
        raise NotImplementedError

    def run(self, session: Session, job: ParseJob, target: Target, account: ParserAccount | None) -> list[ParsedEvent]:
        raise NotImplementedError

    def sync_memberships(self, session: Session) -> dict:
        return {"checked": 0, "linked": 0}
