"""Human approval role: the only path to a send, trash, create-event or delete-event write."""

from __future__ import annotations

import logging

from src.agents.calendar_agent import CalendarAgent
from src.observability.telemetry import security_event_counter
from src.schemas.errors import ResearchError, error_view

logger = logging.getLogger(__name__)


class HumanApprovalAgent:
    """Applies a user's approve/reject decision to a stored send, trash, create-event or delete-event action.

    The only role that writes to the mailbox or calendar, and only after an
    exact-payload approval.
    """

    name = "human_approval"

    def __init__(self, store, backend, settings):
        self.store, self.backend, self.settings = store, backend, settings

    def _execute(self, action):
        """Run the one provider write that matches the approved action kind."""
        kind = action["kind"]
        if kind == "send_email":
            return self.backend.send(action["payload"])
        if kind == "create_event":
            return self.backend.create_event(action["payload"])
        if kind == "delete_event":
            return self.backend.delete_event(action["payload"])
        return self.backend.trash(action["payload"])

    def decide(self, owner, action_id, decision):
        """Validate the decision against the stored action, then execute it at most once.

        Every decision leaves a structured, content-free audit line: who (masked), which
        action and kind, what was decided and how it ended.
        """
        try:
            action = self._decide(owner, action_id, decision)
        except ResearchError as error:
            logger.warning(
                "approval refused action=%s decision=%s code=%s",
                action_id, decision.decision, error.code,
            )
            security_event_counter.add(1, {"event": "approval_refused"})
            raise
        logger.info(
            "approval decided action=%s kind=%s decision=%s state=%s execution=%s owner=%s",
            action_id, action.get("kind"), decision.decision, action.get("state"),
            action.get("execution"), owner[:1] + "***" + owner[owner.find("@"):],
        )
        security_event_counter.add(
            1, {"event": f"approval_{decision.decision}", "kind": str(action.get("kind"))}
        )
        return action

    def _decide(self, owner, action_id, decision):
        action = self.store.action(owner, action_id)
        if action["metadata"]["mode"] != self.backend.mode:
            raise ResearchError(
                "ACTION_CHANGED", "Chế độ tài khoản không khớp yêu cầu.", 409
            )
        if (
            action["version"] != decision.version
            or action["payload_hash"] != decision.payload_hash
        ):
            raise ResearchError(
                "ACTION_CHANGED", "Nội dung đã thay đổi. Hãy xem và duyệt lại.", 409
            )
        # No provider call happens for a rejection, or for an already claimed replay.
        if decision.decision == "approve" and action["state"] == "pending":
            self.backend.check_write(action["kind"])
        action, execute = self.store.claim(
            owner, action_id, decision.decision, decision.version, decision.payload_hash
        )
        if not execute:
            return action
        try:
            slots = action["metadata"].get("calendar_slots", [])
            if slots:
                CalendarAgent(self.backend, self.settings).recheck(
                    slots, action["metadata"]["timezone"]
                )
            result = self._execute(action)
        except ResearchError as error:
            execution = (
                "outcome_unknown"
                if error.code == "ACTION_OUTCOME_UNKNOWN"
                else "failed"
            )
            return self.store.finish(
                owner, action_id, execution, error=error_view(error)
            )
        except Exception:  # noqa: BLE001 - an unclassified write failure must remain outcome_unknown
            return self.store.finish(
                owner,
                action_id,
                "outcome_unknown",
                error={
                    "code": "ACTION_OUTCOME_UNKNOWN",
                    "message": "Chưa xác định kết quả. Không tự động gửi lại.",
                },
            )
        return self.store.finish(owner, action_id, "succeeded", result=result)
