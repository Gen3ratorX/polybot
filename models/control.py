from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProfileControlState:
    control_key: str
    scope: str
    profile_name: str | None
    desired_state: str
    run_once_pending: int
    updated_at: datetime | None = None
    updated_by: str | None = None
    source_chat_id: str | None = None
    source_message_id: int | None = None
    last_command: str | None = None
    notes: str | None = None

    def allows_running_cycle(self) -> bool:
        return self.desired_state != "STOPPED" or self.run_once_pending > 0

    def is_stopped(self) -> bool:
        return self.desired_state == "STOPPED"

    def is_paused(self) -> bool:
        return self.desired_state == "PAUSED"


@dataclass(frozen=True, slots=True)
class ResolvedControlState:
    profile_name: str
    global_state: ProfileControlState
    profile_state: ProfileControlState
    effective_state: str
    run_once_pending: int

    def allows_cycle(self) -> bool:
        return self.effective_state != "STOPPED" and (
            self.effective_state == "RUNNING" or self.run_once_pending > 0
        )

    def should_stop(self) -> bool:
        return self.effective_state == "STOPPED"

    def should_pause(self) -> bool:
        return self.effective_state == "PAUSED" and self.run_once_pending <= 0
