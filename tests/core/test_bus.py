from __future__ import annotations

from eventhub.core.bus import Event, EventBus


def test_subscribe_and_publish() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("drive.removed", lambda event: seen.append(event.name))
    errors = bus.emit("drive.removed", {"uuid": "123"})
    assert errors == []
    assert seen == ["drive.removed"]


def test_wildcard_and_history() -> None:
    bus = EventBus()
    everything: list[Event] = []
    bus.subscribe("*", everything.append)
    bus.emit("one")
    bus.emit("two", {"x": 1})
    assert [e.name for e in everything] == ["one", "two"]
    assert bus.history("two")[0].payload == {"x": 1}
    assert len(bus.history()) == 2


def test_failing_handler_is_isolated() -> None:
    bus = EventBus()
    good: list[str] = []

    def bad(event: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe("x", bad)
    bus.subscribe("x", lambda event: good.append("ok"))
    errors = bus.emit("x")
    assert len(errors) == 1
    assert good == ["ok"]


def test_unsubscribe() -> None:
    bus = EventBus()
    seen: list[str] = []
    unsubscribe = bus.subscribe("x", lambda event: seen.append(event.name))
    unsubscribe()
    bus.emit("x")
    assert seen == []
