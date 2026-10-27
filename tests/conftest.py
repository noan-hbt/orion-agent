from __future__ import annotations

import asyncio
import functools
import inspect
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from orion.core.events import NewEvent, Provenance, Trust
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner, state_of

TEST_TIMEOUT = 60.0
"""A test stuck on an answer that never comes fails rather than freezing the whole run."""


POSIX_ONLY = (
    "test_chatgpt_auth.py::test_sign_in_registers_then_stores_private_credentials",
    "test_claude_code.py::",
    "test_configure.py::test_split_the_guardian_writes_the_file_after_the_owners_yes",
    "test_configure.py::test_through_the_servers_link_the_real_file_is_written",
    "test_configure.py::test_a_copy_it_cannot_write_into_is_replaced",
    "test_delegation.py::",
    "test_deploy_scripts.py::",
    "test_files.py::test_a_file_on_the_server_but_never_a_secret",
    "test_hands.py::",
    "test_hosting.py::test_a_journal_under_an_odd_folder_name_is_backed_up",
    "test_logins.py::test_the_guardian_lends_a_login_once_and_keeps_the_2fa_key",
    "test_logins.py::test_an_export_is_imported_from_the_command_line",
    "test_mobile_push.py::test_the_vapid_key_is_made_once_and_signs",
    "test_module_install.py::",
    "test_private.py::",
    "test_setup.py::test_a_fresh_install_asks_little_and_checks_every_key",
    "test_shell.py::",
    "test_simulate.py::test_the_command",
    "test_telegram_guardian.py::test_an_approval_is_asked_and_checked_by_the_guardian",
    "test_upkeep.py::test_his_secrets_logins_and_machines_from_the_app",
    "test_vault.py::test_the_owner_sets_a_secret_from_telegram_and_the_core_never_sees_it",
    "test_vault_local.py::test_orion_keeps_the_vault_itself",
)
"""Tests of what only a Linux server has (file modes, symlinks, shell scripts, UTF-8 by
default): Orion runs there and CI checks them there; on Windows they are skipped."""


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    posix_only = pytest.mark.skip(reason="Linux only: CI runs it")
    for item in items:
        if sys.platform == "win32" and any(p in item.nodeid for p in POSIX_ONLY):
            item.add_marker(posix_only)
        test = getattr(item, "obj", None)
        if test is None or not inspect.iscoroutinefunction(test):
            continue

        def bounded(test: Any = test) -> Any:
            @functools.wraps(test)
            async def run(*args: Any, **kwargs: Any) -> Any:
                limit = asyncio.timeout(TEST_TIMEOUT)
                try:
                    async with limit:
                        return await test(*args, **kwargs)
                except TimeoutError:
                    if limit.expired():
                        pytest.fail(f"stuck for {TEST_TIMEOUT:.0f}s")
                    raise

            return run

        item.obj = bounded()  # pyright: ignore[reportAttributeAccessIssue]


@pytest.fixture
async def journal(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[SqliteJournal]:
    async with SqliteJournal(tmp_path / "journal.sqlite3", HybridClock("test")) as j:
        yield j
        marker = request.node.get_closest_marker("no_rebuild_check")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if marker is None:
            await rebuild_check(j)


async def rebuild_check(journal: SqliteJournal) -> None:
    """Every projection built on this test's journal must come back the same when rebuilt from
    the journal alone (D81): the whole suite becomes a replay test of every module."""
    different: list[str] = []
    for runner in list(ProjectionRunner.live):
        if runner.journal is not journal:
            continue
        for projection in runner.projections:
            try:
                await ProjectionRunner(journal, [projection]).catch_up()  # a live one may lag
                before = await state_of(projection)
            except Exception:  # noqa: BLE001 - its store was closed by another fixture's teardown
                continue
            await runner.rebuild(projection.name)
            after = await state_of(projection)
            if after != before:
                different.append(f"{projection.name}: {_first_difference(before, after)}")
    if different:
        pytest.fail(
            "projections not rebuilt identically from the journal:\n" + "\n".join(different)
        )


def _first_difference(before: Any, after: Any, path: str = "") -> str:
    if isinstance(before, dict) and isinstance(after, dict):
        b: dict[str, Any] = before  # pyright: ignore[reportUnknownVariableType]
        a: dict[str, Any] = after  # pyright: ignore[reportUnknownVariableType]
        for key in sorted({*b, *a}):
            if b.get(key) != a.get(key):
                return _first_difference(b.get(key), a.get(key), f"{path}.{key}")
    return f"{path or '<state>'}: {before!r:.200} != {after!r:.200}"


def make_event(kind: str = "test.happened", source: str = "test:1", **payload: object) -> NewEvent:
    return NewEvent(
        kind=kind,
        payload=dict(payload),
        provenance=Provenance(source=source, actor="test:runner"),
        trust=Trust.OWNER,
    )


@pytest.fixture(autouse=True)
def modules_run_in_this_process() -> Iterator[None]:
    """A test that starts the real guardian turns modules off for the process (D105)."""
    from orion.modules import installer

    yield
    installer._Process.imports_modules = True  # pyright: ignore[reportPrivateUsage]
