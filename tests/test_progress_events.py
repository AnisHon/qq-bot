from datetime import UTC, datetime
import unittest

from app.progress.events import (
    AgentEvent,
    AgentEventKind,
    AgentEventPayload,
    RequestAgentEventSink,
    TaskIdentity,
    ToolCategory,
)


class AgentEventContractTests(unittest.TestCase):
    def identity(self, request_id: str | None = "message-1") -> TaskIdentity:
        return TaskIdentity.create(
            request_id=request_id,
            group_id="group-1",
            user_id="user-1",
            epoch=4,
            origin="user_message",
        )

    def test_task_identity_has_independent_uuid_and_request_link(self) -> None:
        first = self.identity("onebot-42")
        second = self.identity("onebot-42")
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(first.request_id, "onebot-42")
        self.assertEqual(first.group_id, "group-1")
        self.assertEqual(first.epoch, 4)

    def test_missing_request_id_gets_stable_nonempty_fallback(self) -> None:
        identity = self.identity(None)
        self.assertTrue(identity.request_id)
        self.assertTrue(identity.task_id)

    def test_scope_and_origin_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            TaskIdentity("t", "r", "g", "u", -1, "user_message")
        with self.assertRaises(ValueError):
            TaskIdentity("t", "r", "g", "u", 0, "arbitrary")

    def test_event_requires_version_sequence_and_aware_timestamp(self) -> None:
        identity = self.identity()
        with self.assertRaises(ValueError):
            AgentEvent(2, identity, 1, datetime.now(UTC), AgentEventKind.TASK_STARTED)
        with self.assertRaises(ValueError):
            AgentEvent(1, identity, 0, datetime.now(UTC), AgentEventKind.TASK_STARTED)
        with self.assertRaises(ValueError):
            AgentEvent(1, identity, 1, datetime.now(), AgentEventKind.TASK_STARTED)

    def test_payload_has_only_bounded_typed_fields(self) -> None:
        payload = AgentEventPayload(
            category=ToolCategory.WEB_SEARCH,
            evidence_count=3,
            completeness="partial",
        )
        self.assertEqual(payload.category, ToolCategory.WEB_SEARCH)
        with self.assertRaises(ValueError):
            AgentEventPayload(evidence_count=-1)
        with self.assertRaises(ValueError):
            AgentEventPayload(completeness="whole-page")

    def test_request_sink_assigns_monotonic_sequences_and_isolates_observer(self) -> None:
        seen = []
        sink = RequestAgentEventSink(self.identity(), seen.append)
        self.assertTrue(sink.emit(AgentEventKind.TASK_STARTED))
        self.assertTrue(sink.emit(
            AgentEventKind.TOOL_STARTED,
            AgentEventPayload(category=ToolCategory.WEB_SEARCH),
            tool_call_id="call-1",
        ))
        self.assertEqual([event.sequence for event in seen], [1, 2])
        self.assertEqual(seen[1].identity, sink.identity)

        failing = RequestAgentEventSink(self.identity(), lambda _event: (_ for _ in ()).throw(RuntimeError()))
        self.assertFalse(failing.emit(AgentEventKind.TASK_STARTED))
        self.assertEqual(failing.dropped_events, 1)

    def test_event_count_is_bounded_but_terminal_event_is_admitted(self) -> None:
        seen = []
        sink = RequestAgentEventSink(self.identity(), seen.append, max_events=1)
        self.assertTrue(sink.emit(AgentEventKind.TASK_STARTED))
        self.assertFalse(sink.emit(AgentEventKind.MODEL_REQUEST_STARTED))
        self.assertTrue(sink.emit(
            AgentEventKind.TASK_COMPLETED,
            AgentEventPayload(),
        ))
        self.assertEqual([event.kind for event in seen], [
            AgentEventKind.TASK_STARTED, AgentEventKind.TASK_COMPLETED
        ])
        self.assertEqual(sink.dropped_events, 1)


if __name__ == "__main__":
    unittest.main()
