# Copyright 2026 Marimo. All rights reserved.
"""The worker's runtime operations through the host."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from marimo._ast.app_config import _AppConfig
from marimo._config.config import DEFAULT_CONFIG
from marimo._core.code import code_fingerprint
from marimo._messaging.notification import (
    CellNotification,
    CompletedRunNotification,
    ExecutionNotification,
)
from marimo._messaging.serde import serialize_kernel_message
from marimo._pyodide.pyodide_session import PyodideBridge, PyodideSession
from marimo._runtime.commands import (
    AppMetadata,
    CancelRunCommand,
    CreateNotebookCommand,
    ExecuteCellsCommand,
    ExecuteScratchpadCommand,
)
from marimo._session.model import SessionMode
from marimo._session.notebook import AppFileManager
from marimo._types.ids import CellId_t

if TYPE_CHECKING:
    from pathlib import Path

    from marimo._messaging.notification import NotificationMessage


@pytest.fixture
def bridge(tmp_path: Path):
    filename = tmp_path / "notebook.py"
    filename.write_text("""import marimo
app = marimo.App()
@app.cell
def _():
    x = 1
    return (x,)
""")
    session = PyodideSession(
        AppFileManager(str(filename)),
        SessionMode.EDIT,
        AppMetadata(
            query_params={},
            cli_args={},
            app_config=_AppConfig(),
            filename=str(filename),
        ),
        DEFAULT_CONFIG,
    )
    bridge = PyodideBridge(session)
    yield bridge
    bridge.close()


async def follow(bridge: PyodideBridge) -> list[dict[str, Any]]:
    """Subscribe and keep collecting the host's events as they arrive."""
    events: list[dict[str, Any]] = []

    def receive(raw: str) -> None:
        event = json.loads(raw)
        if event["event"] != "marimo":
            events.append(event)

    await bridge.notebook.subscribe("reader", None, receive)
    return events


def latest(events: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return [event["data"][kind] for event in events if event["event"] == kind][
        -1
    ]


async def call(
    bridge: PyodideBridge,
    operation: str,
    body: dict[str, Any] | None = None,
    *,
    key: str = "run-1",
    **params: str,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "operation": operation,
        "params": {"notebook_id": bridge.notebook.id, **params},
        "headers": {"idempotency-key": key},
    }
    if body is not None:
        message["body"] = body
    return json.loads(await bridge.notebook.call(json.dumps(message)))


async def settle() -> None:
    """Let submissions run their course."""
    for _ in range(4):
        await asyncio.sleep(0)


async def started(bridge: PyodideBridge) -> list[dict[str, Any]]:
    """Follow the host and mark the kernel started, as the page does."""
    events = await follow(bridge)
    bridge.session.started.set()
    await settle()
    return events


def queued(bridge: PyodideBridge) -> list[Any]:
    queue = bridge.session._queue_manager.control_queue
    commands = []
    while not queue.empty():
        commands.append(queue.get_nowait())
    return commands


def deliver(bridge: PyodideBridge, notification: NotificationMessage) -> None:
    bridge.session._on_message(serialize_kernel_message(notification))


def planned(cell_id: str, run_id: str, execution_id: str = "e1"):
    return ExecutionNotification(
        cell_id=CellId_t(cell_id),
        run_id=run_id,
        execution_id=execution_id,
        phase="planned",
        code="x = 1",
        code_fingerprint=code_fingerprint("x = 1"),
    )


def finished(cell_id: str, run_id: str, execution_id: str = "e1"):
    return [
        CellNotification(
            cell_id=CellId_t(cell_id),
            run_id=run_id,
            execution_id=execution_id,
            status="running",
        ),
        CellNotification(
            cell_id=CellId_t(cell_id),
            run_id=run_id,
            execution_id=execution_id,
            status="idle",
        ),
        ExecutionNotification(
            cell_id=CellId_t(cell_id),
            run_id=run_id,
            execution_id=execution_id,
            phase="completed",
        ),
        CompletedRunNotification(run_id=run_id, outcome="succeeded"),
    ]


async def test_the_runtime_runs_once_the_page_starts_the_kernel(bridge):
    events = await follow(bridge)
    notebook = latest(events, "notebook")
    assert {"runtime.run", "runtime.interrupt"} <= set(notebook["operations"])
    assert latest(events, "runtime")["status"] == "starting"
    reply = await call(bridge, "runtime.run", {"code": "1"})
    assert reply["status"] == 409

    bridge.session.started.set()
    await settle()
    runtime = latest(events, "runtime")
    assert runtime["status"] == "running"
    assert runtime["marimo_version"]


async def test_running_a_cell_loads_its_imports_and_queues_the_command(
    bridge,
):
    loaded: list[str] = []

    async def load(code: str) -> None:
        loaded.append(code)

    bridge.notebook.set_package_loader(load)
    bridge.session.instantiated = True
    events = await started(bridge)
    cell = latest(events, "cell")

    reply = await call(bridge, "runtime.run", {"cells": [{"id": cell["id"]}]})
    assert reply["status"] == 202
    run_id = reply["body"]["id"]
    assert latest(events, "run")["status"] == "queued"
    await settle()
    [command] = queued(bridge)
    assert isinstance(command, ExecuteCellsCommand)
    assert command.run_id == run_id
    assert command.allow_stdin
    [request] = command.execution_requests
    assert (request.cell_id, request.code, request.code_fingerprint) == (
        cell["id"],
        "x = 1",
        cell["code_fingerprint"],
    )
    assert loaded == ["x = 1"]

    deliver(bridge, planned(cell["id"], run_id))
    for notification in finished(cell["id"], run_id):
        deliver(bridge, notification)
    execution = latest(events, "execution")
    assert execution["cell_id"] == cell["id"]
    assert execution["status"] == "completed"
    assert execution["code_fingerprint"] == cell["code_fingerprint"]
    run = latest(events, "run")
    assert (run["status"], run["outcome"]) == ("completed", "succeeded")


async def test_scratch_code_runs_against_the_document(bridge):
    bridge.session.instantiated = True
    await started(bridge)
    reply = await call(bridge, "runtime.run", {"code": "x + 1"})
    await settle()
    [command] = queued(bridge)
    assert isinstance(command, ExecuteScratchpadCommand)
    assert command.code == "x + 1"
    assert command.run_id == reply["body"]["id"]
    assert command.allow_stdin
    assert [cell.code for cell in command.notebook_cells] == ["x = 1"]


async def test_a_run_before_instantiation_registers_the_cells_first(bridge):
    assert not bridge.session.instantiated
    events = await started(bridge)
    cell = latest(events, "cell")
    await call(bridge, "runtime.run", {"cells": [{"id": cell["id"]}]})
    await settle()
    registration, command = queued(bridge)
    assert isinstance(registration, CreateNotebookCommand)
    assert registration.auto_run is False
    assert isinstance(command, ExecuteCellsCommand)
    assert bridge.session.instantiated


async def test_interrupting_a_queued_run_cancels_it_in_the_kernel(bridge):
    bridge.session.instantiated = True
    events = await started(bridge)
    cell = latest(events, "cell")
    reply = await call(bridge, "runtime.run", {"cells": [{"id": cell["id"]}]})
    run_id = reply["body"]["id"]
    await settle()
    queued(bridge)

    reply = await call(
        bridge, "runtime.interrupt", key="stop-1", run_id=run_id
    )
    assert reply["status"] == 202
    [command] = queued(bridge)
    assert isinstance(command, CancelRunCommand)
    assert command.run_id == run_id
    deliver(
        bridge, CompletedRunNotification(run_id=run_id, outcome="interrupted")
    )
    run = latest(events, "run")
    assert (run["status"], run["outcome"]) == ("completed", "interrupted")


async def test_interrupting_a_started_run_is_left_to_the_page(bridge):
    bridge.session.instantiated = True
    events = await started(bridge)
    cell = latest(events, "cell")
    reply = await call(bridge, "runtime.run", {"cells": [{"id": cell["id"]}]})
    run_id = reply["body"]["id"]
    await settle()
    queued(bridge)
    deliver(bridge, planned(cell["id"], run_id))

    reply = await call(
        bridge, "runtime.interrupt", key="stop-1", run_id=run_id
    )
    assert reply["status"] == 202
    assert queued(bridge) == []


async def test_interrupting_before_submission_ends_the_run(bridge):
    gate = asyncio.Event()

    async def load(code: str) -> None:
        del code
        await gate.wait()

    bridge.notebook.set_package_loader(load)
    bridge.session.instantiated = True
    events = await started(bridge)
    cell = latest(events, "cell")
    reply = await call(bridge, "runtime.run", {"cells": [{"id": cell["id"]}]})
    run_id = reply["body"]["id"]
    await settle()
    await call(bridge, "runtime.interrupt", key="stop-1", run_id=run_id)
    await settle()
    gate.set()
    await settle()
    assert queued(bridge) == []
    run = latest(events, "run")
    assert (run["status"], run["outcome"]) == ("completed", "interrupted")


async def test_runs_the_kernel_schedules_appear_on_the_stream(bridge):
    bridge.session.instantiated = True
    events = await started(bridge)
    cell = latest(events, "cell")
    deliver(bridge, planned(cell["id"], "legacy", "e9"))
    for notification in finished(cell["id"], "legacy", "e9"):
        deliver(bridge, notification)
    runs = [
        event["data"]["run"] for event in events if event["event"] == "run"
    ]
    assert runs[0]["id"] == "legacy"
    assert (runs[-1]["status"], runs[-1]["outcome"]) == (
        "completed",
        "succeeded",
    )
    assert latest(events, "execution")["id"] == "e9"
