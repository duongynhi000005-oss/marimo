# Copyright 2026 Marimo. All rights reserved.
"""The worker's host document, commands, and event subscriptions."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import msgspec

from marimo import _loggers
from marimo._core import backend as be, protocol
from marimo._core.calls import OperationCall, dispatch
from marimo._core.document import Document
from marimo._core.errors import Conflict, Invalid, NotFound
from marimo._core.executions import Executions
from marimo._core.host import Host
from marimo._core.ids import mint
from marimo._core.notebook import Notebook
from marimo._core.operations import Mutations, problem
from marimo._messaging.notebook.outputs import scratchpad_snapshot
from marimo._messaging.notification import (
    NotebookDocumentTransactionNotification,
)
from marimo._messaging.serde import (
    deserialize_kernel_message,
    serialize_kernel_message,
)
from marimo._pyodide.kernel_ready import kernel_ready
from marimo._runtime.commands import (
    CancelRunCommand,
    ExecuteCellCommand,
    ExecuteCellsCommand,
    ExecuteScratchpadCommand,
)
from marimo._runtime.layout.layout import (
    LayoutConfig,
    layout_config_to_data_uri,
)
from marimo._server.host import document as documents
from marimo._server.host.events import notification_event, serialize_event
from marimo._server.host.translate import (
    RunTranslator,
    RunTranslators,
    variable_messages,
)
from marimo._session.model import SessionMode
from marimo._session.notebook.writer import NotebookWriter, cells_to_ir
from marimo._types.ids import CellId_t
from marimo._version import __version__

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from typing import Any

    from marimo._core.stream import Sink
    from marimo._messaging.notification import NotificationMessage
    from marimo._messaging.types import KernelMessage
    from marimo._pyodide.pyodide_session import PyodideSession
    from marimo._runtime.commands import CommandMessage

LOGGER = _loggers.marimo_logger()

_OPERATIONS = (
    "notebook.edit",
    "notebook.save",
    "runtime.run",
    "runtime.interrupt",
)


@dataclass
class _WorkerRun:
    translator: RunTranslator
    task: asyncio.Task[None] | None = None
    submitted: bool = False


class WorkerBackend:
    """Run the notebook on the worker's kernel and report through the host.

    The kernel shares the worker's thread, so a started run cannot be
    interrupted from here. The page raises the interrupt buffer for a
    running run; this backend cancels a run the kernel has not started.
    """

    def __init__(
        self,
        session: PyodideSession,
        observe: Callable[[be.Message], None],
        load_packages: Callable[[str], Awaitable[None]],
    ) -> None:
        self._session = session
        self._observe = observe
        self._load_packages = load_packages
        self._translators = RunTranslators()
        self._runs: dict[protocol.RunId, _WorkerRun] = {}
        self._announce: asyncio.Task[None] | None = None
        session.consumers.append(self._notify)

    def close(self) -> None:
        if self._notify in self._session.consumers:
            self._session.consumers.remove(self._notify)
        if self._announce is not None:
            self._announce.cancel()
        for run in list(self._runs.values()):
            if run.task is not None:
                run.task.cancel()

    # Backend commands

    def attach(self, runtime_id: protocol.RuntimeId) -> None:
        """Adopt the page's kernel as `runtime_id`, running once it starts.

        The notebook exists from the first subscription, before the page
        starts the kernel, so the runtime stays `starting` until then.
        """
        self._announce = asyncio.get_running_loop().create_task(
            self._announce_running(runtime_id),
            name=f"worker.runtime.{runtime_id}",
        )

    async def _announce_running(self, runtime_id: protocol.RuntimeId) -> None:
        await self._session.started.wait()
        self._observe(be.Running(runtime_id, __version__))

    def start(self, runtime_id: protocol.RuntimeId) -> None:
        self.attach(runtime_id)

    def stop(self, runtime_id: protocol.RuntimeId) -> None:
        del runtime_id
        LOGGER.warning("The worker's kernel stops with its page")

    def run(self, run_id: protocol.RunId, code: str) -> None:
        run = self._track(run_id, code)
        cells, outputs = scratchpad_snapshot(
            self._session.app_manager.app.cell_manager.document,
            self._session.session_view,
        )
        self._submit(
            run,
            ExecuteScratchpadCommand(
                code=code,
                request=None,
                notebook_cells=cells,
                cell_outputs=outputs,
                run_id=run_id,
                allow_stdin=True,
            ),
            code,
        )

    def run_cells(
        self,
        run_id: protocol.RunId,
        document: Document,
        cell_ids: Sequence[protocol.CellId],
    ) -> None:
        run = self._track(run_id, None)
        cells = {
            cell.id: ExecuteCellCommand(
                cell_id=CellId_t(cell.id),
                code=cell.code,
                code_fingerprint=cell.code_fingerprint,
            )
            for cell in document
        }
        requests = [cells[cell_id] for cell_id in cell_ids]
        self._submit(
            run,
            ExecuteCellsCommand(
                execution_requests=requests,
                request=None,
                run_id=run_id,
                allow_stdin=True,
            ),
            "\n".join(request.code for request in requests),
        )

    def interrupt(self, run_id: protocol.RunId) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        if not run.submitted:
            if run.task is not None:
                run.task.cancel()
            return
        if not run.translator.started:
            # The kernel discards a queued run when its command arrives.
            self._session.put_control_request(CancelRunCommand(run_id=run_id))
        # A started run is interrupted by the page through the interrupt buffer.

    def forget(self, run_id: protocol.RunId) -> None:
        self._translators.forget(run_id)

    # Submission

    def _track(self, run_id: protocol.RunId, code: str | None) -> _WorkerRun:
        run = _WorkerRun(RunTranslator(run_id, code))
        self._runs[run_id] = run
        return run

    def _submit(
        self, run: _WorkerRun, command: CommandMessage, code: str
    ) -> None:
        """Queue `command` once the packages `code` imports are loaded."""

        async def submit() -> None:
            await self._load_packages(code)
            if not self._session.instantiated:
                from marimo._pyodide.bootstrap import instantiate

                # Register the notebook's cells without running them.
                instantiate(self._session, auto_instantiate=False)
            self._translators.add(run.translator)
            run.submitted = True
            self._session.put_control_request(command)

        run.task = asyncio.get_running_loop().create_task(
            submit(), name=f"worker.run.{run.translator.run_id}"
        )
        run.task.add_done_callback(lambda task: self._settle(run, task))

    def _settle(self, run: _WorkerRun, task: asyncio.Task[None]) -> None:
        """End a run whose submission was cancelled or failed."""
        if task.cancelled():
            self._finish(run, interrupted=True)
            return
        exception = task.exception()
        if exception is None:
            return
        LOGGER.error("Run failed in the worker: %s", exception)
        self._finish(
            run,
            error=protocol.Error(
                kind="internal",
                name=None,
                message=str(exception) or type(exception).__name__,
                traceback=None,
                source="marimo",
                severity="error",
                range=None,
                related=(),
            ),
        )

    def _finish(
        self,
        run: _WorkerRun,
        *,
        error: protocol.Error | None = None,
        interrupted: bool = False,
    ) -> None:
        if self._runs.get(run.translator.run_id) is run:
            del self._runs[run.translator.run_id]
        if interrupted:
            run.translator.interrupted = True
        for message in run.translator.finish(error=error):
            self._observe(message)

    # Kernel notifications

    def _notify(self, message: KernelMessage) -> None:
        notification = deserialize_kernel_message(message)
        for reported in variable_messages(notification):
            self._observe(reported)
        translator, reports = self._translators.feed(notification)
        for reported in reports:
            self._observe(reported)
        if translator is None or not translator.completed:
            return
        run = self._runs.get(translator.run_id)
        if run is not None and run.translator is translator:
            self._finish(run)
        else:
            for reported in translator.finish():
                self._observe(reported)


class PyodideNotebook:
    """One session's notebook, independent of the worker RPC and React."""

    def __init__(self, session: PyodideSession) -> None:
        self._session = session
        self._subscriptions: dict[str, Callable[[], None]] = {}
        self._closed = False
        self._save_lock = asyncio.Lock()
        self._persist: Callable[[str, str], Awaitable[None]] | None = None
        self._load_packages: Callable[[str], Awaitable[None]] | None = None
        self._backend: WorkerBackend | None = None
        self.host = Host(operations=_OPERATIONS)
        project_id = mint(protocol.ProjectId, "p")
        self.id = mint(protocol.NotebookId, "n")
        self.host.update_project(
            protocol.Project(
                id=project_id,
                name="WASM",
                root=None,
                operations=(),
                status="indexed",
                message=None,
            )
        )
        filename = session.app_manager.filename
        self.host.add_notebook(
            protocol.Notebook(
                id=self.id,
                project_id=project_id,
                name=Path(filename).name if filename else "Untitled",
                path=None,
                updated_at=None,
                operations=_OPERATIONS
                if session.mode is SessionMode.EDIT
                else (),
            ),
            self._initialize,
        )

        self.mutations = Mutations(self.host)

    def _initialize(self, record: protocol.Notebook, sink: Sink) -> Notebook:
        cells, config = documents.from_file_manager(self._session.app_manager)
        document = Document(cells, config)
        backend = WorkerBackend(
            self._session,
            lambda message: self._observe(record.id, message),
            self._load_packages_for,
        )
        self._backend = backend
        notebook = Notebook(
            record.id,
            document,
            Executions(),
            backend,
            sink,
            saved_etag=document.etag,
        )
        # The page starts the kernel; the notebook adopts it as its runtime.
        notebook.adopt(backend.attach)
        return notebook

    def _observe(self, notebook_id: str, message: be.Message) -> None:
        try:
            notebook = self.host.peek_notebook(notebook_id)
        except NotFound:
            return
        if notebook is not None:
            notebook.observe(message)

    async def _load_packages_for(self, code: str) -> None:
        if self._load_packages is not None:
            await self._load_packages(code)

    def set_package_loader(
        self, load: Callable[[str], Awaitable[None]]
    ) -> None:
        """Load the packages a run's code imports before the kernel runs it."""
        self._load_packages = load

    def refresh(self) -> None:
        """Observe changes from session operations not yet exposed by the host."""
        if self._closed:
            return
        notebook = self.host.peek_notebook(self.id)
        if notebook is not None:
            cells, config = documents.from_file_manager(
                self._session.app_manager
            )
            notebook.replace(cells, config)

    async def subscribe(
        self,
        subscription_id: str,
        cursor: str | None,
        emit: Callable[[str], None],
    ) -> None:
        if self._closed:
            raise RuntimeError("The notebook is closed")
        await self.host.notebook(self.id)
        if self._closed:
            raise RuntimeError("The notebook is closed")
        self.refresh()
        self.unsubscribe(subscription_id)

        def notify(notification: NotificationMessage) -> None:
            emit(serialize_event(notification_event(self.id, notification)))

        def consume(message: KernelMessage) -> None:
            notify(deserialize_kernel_message(message))

        # Snapshot and subscriptions are established in the same event-loop turn.
        initial = self.host.events(cursor, notebook_id=self.id)
        unsubscribe = self.host.subscribe(
            lambda event: emit(serialize_event(event)),
            notebook_id=self.id,
        )
        session = self._session
        session.consumers.append(consume)

        def close() -> None:
            unsubscribe()
            session.consumers.remove(consume)

        self._subscriptions[subscription_id] = close
        try:
            for event in initial:
                emit(serialize_event(event))
            # As on a server connection, kernel-ready and the session's
            # retained notifications follow the snapshot.
            view = session.session_view
            notify(
                kernel_ready(
                    session.app_manager, view if session.instantiated else None
                )
            )
            for notification in view.snapshot_notifications():
                notify(notification)
        except BaseException:
            self.unsubscribe(subscription_id)
            raise

    def unsubscribe(self, subscription_id: str) -> None:
        unsubscribe = self._subscriptions.pop(subscription_id, None)
        if unsubscribe is not None:
            unsubscribe()

    async def call(self, raw: str) -> str:
        """Serve an operation call and reply with its status and body."""
        self.mutations.expire()
        body: msgspec.Struct | None
        try:
            call = msgspec.json.decode(raw, type=OperationCall)
        except msgspec.DecodeError as e:
            failure = problem(400, "invalid-request", str(e))
            status, body = failure.status, failure
        else:
            status, body = await dispatch(self.mutations, self, call)
        return msgspec.json.encode({"status": status, "body": body}).decode()

    async def edit(
        self,
        notebook_id: str,
        changes: Sequence[protocol.Change],
        *,
        request_id: str,
    ) -> protocol.EditResponse:
        notebook = await self.host.notebook(notebook_id)
        self.refresh()
        result = notebook.edit(changes, request_id=request_id)
        self._apply_to_session(notebook, notify=True)
        return result

    def _apply_to_session(self, notebook: Notebook, *, notify: bool) -> None:
        transaction, update = documents.apply_to_document(
            self._session.app_manager.app.cell_manager.document,
            notebook.document,
        )
        if transaction is not None and notify:
            self._session._on_message(
                serialize_kernel_message(
                    NotebookDocumentTransactionNotification(
                        transaction=transaction
                    )
                )
            )
        # Before instantiation the kernel receives the latest app cells.
        if update is not None and self._session.instantiated:
            self._session.put_control_request(update)

    async def update_config(self, config: dict[str, Any]) -> None:
        notebook = await self.host.notebook(self.id)
        self._session.app_manager.app.update_config(config)
        notebook.replace(
            list(notebook.document),
            self._session.app_manager.app.config.asdict_difference(),
        )

    async def save_app_config(self, config: dict[str, Any]) -> None:
        """Apply the config and write the notebook, as the server does."""
        await self.update_config(config)
        if self._persist is not None:
            await self.save(
                self.id,
                if_match=None,
                decisions={},
                request_id=f"app-config:{uuid.uuid4()}",
            )

    async def set_layout(self, raw: str) -> None:
        layout = msgspec.json.decode(raw, type=LayoutConfig | None)
        # Inline layouts survive URL/local-storage reloads along with the source.
        await self.update_config(
            {
                "layout_file": layout_config_to_data_uri(layout)
                if layout
                else None,
            }
        )

    def set_persistence(
        self, persist: Callable[[str, str], Awaitable[None]]
    ) -> None:
        self._persist = persist

    async def save(
        self,
        notebook_id: str,
        *,
        if_match: Sequence[str] | None,
        decisions: Mapping[str, str],
        request_id: str,
    ) -> protocol.EditResponse:
        notebook = await self.host.notebook(notebook_id)
        async with self._save_lock:
            if decisions:
                raise Invalid("Worker saves do not require decisions")
            if self._persist is None:
                raise Conflict("Notebook persistence is unavailable")
            if if_match is not None:
                notebook.require_revision(if_match)
            revision = notebook.document
            manager = self._session.app_manager
            path = Path(manager.filename or "notebook.py")
            ir = cells_to_ir(
                (documents.to_notebook_cell(cell) for cell in revision),
                revision.app_config,
                header=manager.app._app._header,
            )
            try:
                contents = NotebookWriter().write_ir(
                    path, notebook=ir, persist=False
                )
                await self._persist(contents, str(path))
            except Exception as e:
                raise Conflict(f"The notebook cannot be saved: {e}") from e
            document = notebook.mark_saved(
                revision.etag, request_id=request_id
            )
            return protocol.EditResponse(document=document, cells={})

    def close(self) -> None:
        self._closed = True
        if self._backend is not None:
            self._backend.close()
        for subscription_id in list(self._subscriptions):
            self.unsubscribe(subscription_id)
        self.mutations.replies.clear()
        self.host.remove_notebook(self.id)
