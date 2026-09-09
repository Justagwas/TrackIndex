from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from trackindex.core.models import (
    AuditResult,
    ChangePlan,
    LibraryRecord,
    OrderAuthority,
    SessionCommand,
    StorageMode,
    TrackMetadata,
)
from trackindex.core.self_updater import PreparedUpdate, UpdateCheckData


@dataclass(slots=True)
class LibrarySnapshot:
    record_key: tuple[str, str, str, bool]
    audit: AuditResult
    metadata: dict[str, TrackMetadata]
    selected_authority: OrderAuthority | None
    scroll_value: int = 0


@dataclass(frozen=True, slots=True)
class LibraryWorkToken:
    generation: int
    library_id: str


@dataclass(frozen=True, slots=True)
class PlanOperationContext:
    operation: Literal["reorder", "import", "delete"]
    generation: int
    library_id: str
    before: tuple[str, ...]
    mode: StorageMode
    playlist_name: str
    after: tuple[str, ...] | None = None
    moved: int = 1


@dataclass(frozen=True, slots=True)
class TransactionOperationContext:
    kind: Literal["forward", "undo", "redo"]
    command: SessionCommand
    plan: ChangePlan
    before: tuple[str, ...] | None = None
    moved: int = 1
    action: str = ""

    @property
    def library_id(self) -> str:
        return self.command.library_id


@dataclass(slots=True)
class MutationCoordinationService:
    plan_task_id: str = ""
    plan_context: PlanOperationContext | None = None
    transaction_task_id: str = ""
    transaction_context: TransactionOperationContext | None = None

    def begin_plan(self, task_id: str, context: PlanOperationContext) -> None:
        if self.plan_task_id or self.transaction_task_id:
            raise RuntimeError("A mutation lifecycle is already active.")
        self.plan_task_id = task_id
        self.plan_context = context

    def accept_plan(self, task_id: str) -> PlanOperationContext | None:
        if not task_id or task_id != self.plan_task_id:
            return None
        context = self.plan_context
        self.plan_task_id = ""
        self.plan_context = None
        return context

    def abandon_plan(self) -> None:
        self.plan_task_id = ""
        self.plan_context = None

    def begin_transaction(
        self,
        task_id: str,
        context: TransactionOperationContext,
    ) -> None:
        if self.plan_task_id or self.transaction_task_id:
            raise RuntimeError("A filesystem transaction is already active.")
        self.transaction_task_id = task_id
        self.transaction_context = context

    def accept_transaction(
        self,
        task_id: str,
    ) -> TransactionOperationContext | None:
        if not task_id or task_id != self.transaction_task_id:
            return None
        context = self.transaction_context
        self.transaction_task_id = ""
        self.transaction_context = None
        return context

    def abandon_transaction(self) -> None:
        self.transaction_task_id = ""
        self.transaction_context = None


@dataclass(slots=True)
class UpdateCoordinationService:
    manual: bool = False
    active_check: UpdateCheckData | None = None
    prepared: PreparedUpdate | None = None

    def begin_check(self, manual: bool) -> None:
        self.manual = bool(manual)

    def record_check(self, check: UpdateCheckData) -> None:
        self.active_check = check

    def stage(self, prepared: PreparedUpdate) -> None:
        self.prepared = prepared

    def take_prepared(self) -> PreparedUpdate | None:
        prepared = self.prepared
        self.prepared = None
        return prepared

    def fallback_url(self) -> str:
        return self.active_check.page_url if self.active_check is not None else ""


class LibraryCoordinationService:
    def __init__(self, snapshot_limit: int = 12) -> None:
        self.snapshot_limit = max(1, snapshot_limit)
        self.snapshots: OrderedDict[str, LibrarySnapshot] = OrderedDict()
        self.prefetch_attempted: set[str] = set()

    @staticmethod
    def record_key(record: LibraryRecord) -> tuple[str, str, str, bool]:
        return (
            str(record.folder).casefold(),
            record.canonical_playlist.casefold(),
            record.storage_mode,
            record.setup_completed,
        )

    def snapshot_for(self, record: LibraryRecord) -> LibrarySnapshot | None:
        snapshot = self.snapshots.get(record.library_id)
        if snapshot is None:
            return None
        if snapshot.record_key != self.record_key(record):
            self.snapshots.pop(record.library_id, None)
            return None
        self.snapshots.move_to_end(record.library_id)
        return snapshot

    def store(
        self,
        record: LibraryRecord,
        audit: AuditResult,
        metadata: dict[str, TrackMetadata],
        authority: OrderAuthority | None,
        scroll_value: int = 0,
    ) -> LibrarySnapshot:
        snapshot = LibrarySnapshot(
            self.record_key(record),
            audit,
            dict(metadata),
            authority,
            scroll_value,
        )
        self.snapshots[record.library_id] = snapshot
        self.snapshots.move_to_end(record.library_id)
        while len(self.snapshots) > self.snapshot_limit:
            self.snapshots.popitem(last=False)
        return snapshot

    def discard(self, library_id: str) -> None:
        self.snapshots.pop(library_id, None)
        self.prefetch_attempted.discard(library_id)

    def invalidate_if_changed(self, record: LibraryRecord) -> None:
        snapshot = self.snapshots.get(record.library_id)
        if snapshot is not None and snapshot.record_key != self.record_key(record):
            self.snapshots.pop(record.library_id, None)

    def available_prefetch_slots(self, active_ids: set[str]) -> int:
        occupied = active_ids | set(self.snapshots)
        return max(0, self.snapshot_limit - len(occupied))

    def should_prefetch(self, record: LibraryRecord, active_ids: set[str]) -> bool:
        return bool(
            record.setup_completed
            and record.library_id not in active_ids
            and record.library_id not in self.snapshots
            and record.library_id not in self.prefetch_attempted
        )

    def mark_prefetch_attempted(self, library_id: str) -> None:
        self.prefetch_attempted.add(library_id)

    def update_metadata(
        self,
        record: LibraryRecord,
        values: dict[str, TrackMetadata],
    ) -> None:
        snapshot = self.snapshots.get(record.library_id)
        if snapshot is not None and snapshot.record_key == self.record_key(record):
            snapshot.metadata.update(values)

    @staticmethod
    def owns_result(
        token: LibraryWorkToken | None,
        task_generation: int | None,
        result_generation: int,
        current_generation: int,
        current_library_id: str | None,
    ) -> bool:
        return bool(
            token is not None
            and task_generation is not None
            and current_library_id
            and result_generation == task_generation
            and result_generation == token.generation
            and result_generation == current_generation
            and token.library_id == current_library_id
        )
