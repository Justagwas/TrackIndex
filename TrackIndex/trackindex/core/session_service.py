from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_json
from .config import (
    SESSION_RECOVERY_FOLDER,
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_PLAYLIST_EXTENSIONS,
)
from .filesystem import is_link_like
from .integrity import verify_content
from .models import SessionCommand, SessionPresence, SessionRecoveryRecord, StorageMode
from .paths import is_direct_child, sessions_dir
from .presence import recovery_path, validate_recovery_ancestors
from .recycle_service import recycle_files
from .windows_names import is_valid_windows_name

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_SESSION_BYTES = 64 * 1024 * 1024
_MAX_SESSION_TRACKS = 100000


def _encode(value: bytes | None) -> str | None:
    return base64.b64encode(value).decode("ascii") if value is not None else None


def _decode(value: object) -> bytes | None:
    if value is None:
        return None
    encoded = str(value)
    if len(encoded) > _MAX_SESSION_BYTES * 4 // 3 + 4:
        raise ValueError("Session playlist data is too large.")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Session playlist data is malformed.") from exc


def _strict_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _pairs(value: object, folder: Path) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) > _MAX_SESSION_TRACKS:
        raise ValueError("Session filename data is malformed.")
    result: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("Session filename data is malformed.")
        track_id, name = str(item[0]), str(item[1])
        if (
            not _SAFE_ID.fullmatch(track_id)
            or track_id in seen_ids
            or not is_valid_windows_name(name)
            or Path(name).suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS
            or not is_direct_child(folder / name, folder)
            or name.casefold() in seen_names
        ):
            raise ValueError("Session filename data is unsafe or duplicated.")
        seen_ids.add(track_id)
        seen_names.add(name.casefold())
        result.append((track_id, name))
    return tuple(result)


def _identifier_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_SESSION_TRACKS:
        raise ValueError("Session track order is malformed.")
    return tuple(str(item) for item in value)


def command_to_dict(command: SessionCommand) -> dict[str, object]:
    return {
        "command_id": command.command_id,
        "library_id": command.library_id,
        "folder": str(command.folder),
        "track_ids": list(command.track_ids),
        "before_order": list(command.before_order),
        "after_order": list(command.after_order),
        "before_names": [list(item) for item in command.before_names],
        "after_names": [list(item) for item in command.after_names],
        "playlist_path": str(command.playlist_path) if command.playlist_path is not None else None,
        "before_playlist_bytes": _encode(command.before_playlist_bytes),
        "after_playlist_bytes": _encode(command.after_playlist_bytes),
        "playlist_created": command.playlist_created,
        "before_storage_mode": command.before_storage_mode,
        "after_storage_mode": command.after_storage_mode,
        "before_setup_completed": command.before_setup_completed,
        "after_setup_completed": command.after_setup_completed,
        "before_canonical_playlist": command.before_canonical_playlist,
        "after_canonical_playlist": command.after_canonical_playlist,
        "presence": [
            {
                "track_id": item.track_id,
                "recovery_name": item.recovery_name,
                "size": item.size,
                "sha256": item.sha256,
            }
            for item in command.presence
        ],
    }


def _presence(value: object, folder: Path, command_id: str) -> tuple[SessionPresence, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_SESSION_TRACKS:
        raise ValueError("Session recovery data is malformed.")
    result: list[SessionPresence] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("Session recovery data is malformed.")
        track_id = str(raw.get("track_id", ""))
        name = str(raw.get("recovery_name", ""))
        try:
            size = int(raw.get("size", -1))
            sha256 = str(raw.get("sha256", ""))
            recovery_path(folder, command_id, name)
        except (TypeError, ValueError):
            raise ValueError("Session recovery data is unsafe.") from None
        if (
            not _SAFE_ID.fullmatch(track_id)
            or track_id in seen
            or size < 0
            or (sha256 and not _SHA256.fullmatch(sha256))
        ):
            raise ValueError("Session recovery data is unsafe or duplicated.")
        seen.add(track_id)
        result.append(SessionPresence(track_id, name, size, sha256.casefold()))
    return tuple(result)


def command_from_dict(payload: dict[str, object]) -> SessionCommand:
    command_id = str(payload.get("command_id", ""))
    library_id = str(payload.get("library_id", ""))
    if not _SAFE_ID.fullmatch(command_id) or not _SAFE_ID.fullmatch(library_id):
        raise ValueError("Session identifiers are unsafe.")
    raw_folder = str(payload.get("folder", "")).strip()
    if not raw_folder or not Path(raw_folder).is_absolute():
        raise ValueError("Session folder path is unsafe.")
    if is_link_like(Path(raw_folder)):
        raise ValueError("Session folder cannot use a link or junction.")
    folder = Path(raw_folder).resolve(strict=False)
    before_names = _pairs(payload.get("before_names", []), folder)
    after_names = _pairs(payload.get("after_names", []), folder)
    before_ids = {item[0] for item in before_names}
    after_ids = {item[0] for item in after_names}
    identities = before_ids | after_ids
    track_ids = _identifier_list(payload.get("track_ids", []))
    before_order = _identifier_list(payload.get("before_order", []))
    after_order = _identifier_list(payload.get("after_order", []))
    if (
        len(track_ids) != len(set(track_ids))
        or set(track_ids) != identities
        or len(before_order) != len(set(before_order))
        or len(after_order) != len(set(after_order))
        or set(before_order) != before_ids
        or set(after_order) != after_ids
    ):
        raise ValueError("Session track identities do not match.")
    after = _decode(payload.get("after_playlist_bytes"))
    before = _decode(payload.get("before_playlist_bytes"))
    raw_playlist_path = payload.get("playlist_path")
    playlist_path = Path(str(raw_playlist_path)) if raw_playlist_path else None
    if playlist_path is not None and (
        not is_direct_child(playlist_path, folder)
        or playlist_path.suffix.casefold()
        not in SUPPORTED_PLAYLIST_EXTENSIONS
    ):
        raise ValueError("Session playlist path points outside its library.")
    playlist_created = _strict_bool(payload.get("playlist_created"), False)
    if playlist_path is None and (before is not None or after is not None or playlist_created):
        raise ValueError("Session playlist state has no safe target.")
    if playlist_created and (before is not None or after is None):
        raise ValueError("Session playlist creation state is inconsistent.")
    before_mode = str(payload.get("before_storage_mode", StorageMode.BOTH.value))
    after_mode = str(payload.get("after_storage_mode", StorageMode.BOTH.value))
    if before_mode not in {item.value for item in StorageMode} or after_mode not in {
        item.value for item in StorageMode
    }:
        raise ValueError("Session ordering mode is invalid.")
    before_canonical = str(payload.get("before_canonical_playlist", ""))
    after_canonical = str(payload.get("after_canonical_playlist", ""))
    for canonical in (before_canonical, after_canonical):
        if canonical and (
            not is_valid_windows_name(canonical)
            or Path(canonical).suffix.casefold()
            not in SUPPORTED_PLAYLIST_EXTENSIONS
        ):
            raise ValueError("Session canonical playlist name is unsafe.")
    presence = _presence(payload.get("presence", []), folder, command_id)
    changed_ids = before_ids ^ after_ids
    if {item.track_id for item in presence} != changed_ids:
        raise ValueError("Session recovery identities do not match track presence changes.")
    return SessionCommand(
        command_id, library_id, folder,
        track_ids,
        before_order, after_order,
        before_names,
        after_names,
        playlist_path.resolve(strict=False) if playlist_path else None,
        before, after,
        playlist_created,
        before_mode,
        after_mode,
        _strict_bool(payload.get("before_setup_completed"), True),
        _strict_bool(payload.get("after_setup_completed"), True),
        before_canonical,
        after_canonical,
        presence,
    )


@dataclass(slots=True)
class LibraryHistory:
    undo: list[SessionCommand] = field(default_factory=list)
    redo: list[SessionCommand] = field(default_factory=list)
    persisted: bool = False


class SessionManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or sessions_dir()
        self.histories: dict[str, LibraryHistory] = {}
        self._protected_libraries: set[str] = set()
        self._pending_commands: dict[str, str] = {}
        self._recovery_scan_failed = False
        if self.root.exists() and not is_link_like(self.root):
            try:
                self._protected_libraries.update(path.name for path in self.root.iterdir())
            except OSError:
                self._recovery_scan_failed = True

    def protect_recovery(self, library_id: str) -> None:
        self._protected_libraries.add(library_id)

    def _finish_pending(self, library_id: str) -> None:
        self._pending_commands.pop(library_id, None)
        self._protected_libraries.discard(library_id)

    def history(self, library_id: str) -> LibraryHistory:
        return self.histories.setdefault(library_id, LibraryHistory())

    def _path(self, library_id: str) -> Path:
        if not _SAFE_ID.fullmatch(library_id):
            raise ValueError("Library identifier cannot be used for session storage.")
        return self.root / library_id / "session.json"

    def _command_path(self, library_id: str, command_id: str) -> Path:
        if not _SAFE_ID.fullmatch(command_id):
            raise ValueError("Command identifier cannot be used for session storage.")
        return self._path(library_id).parent / "commands" / f"{command_id}.json"

    def _write_command(self, command: SessionCommand) -> None:
        path = self._command_path(command.library_id, command.command_id)
        if path.parent.exists() and is_link_like(path.parent):
            raise OSError("Session command storage cannot use a link or junction.")
        path.parent.mkdir(exist_ok=True)
        atomic_write_json(path, command_to_dict(command))

    def _write(self, library_id: str, *, pending: SessionCommand | None = None, pending_action: str = "") -> None:
        history = self.history(library_id)
        path = self._path(library_id)
        self.root.mkdir(parents=True, exist_ok=True)
        if is_link_like(self.root):
            raise OSError("Session storage cannot use a link or junction.")
        if path.parent.exists() and is_link_like(path.parent):
            raise OSError("Library session storage cannot use a link or junction.")
        path.parent.mkdir(exist_ok=True)
        if pending is not None:
            self._write_command(pending)
        payload = {
            "schema_version": 4,
            "accepted": False,
            "command_ids": [command.command_id for command in history.undo],
            "undo_ids": [command.command_id for command in history.undo],
            "redo_ids": [command.command_id for command in history.redo],
            "pending_id": pending.command_id if pending else None,
            "pending_action": pending_action,
        }
        atomic_write_json(path, payload)
        history.persisted = True

    def prepare(self, command: SessionCommand, action: str = "forward") -> None:
        if action not in {"forward", "undo", "redo"}:
            raise ValueError("Unknown session action.")
        if self._recovery_scan_failed or command.library_id in self._protected_libraries:
            raise ValueError("This library has pending recovery data. Resolve it before making more changes.")
        self.protect_recovery(command.library_id)
        self._pending_commands[command.library_id] = command.command_id
        self._write(command.library_id, pending=command, pending_action=action)

    def abort(self, command: SessionCommand) -> None:
        if self._pending_commands.get(command.library_id) != command.command_id:
            return
        self._write(command.library_id)
        self._finish_pending(command.library_id)

    def commit_forward(self, command: SessionCommand) -> None:
        history = self.history(command.library_id)
        if any(item.command_id == command.command_id for item in (*history.undo, *history.redo)):
            raise ValueError("Session command was already recorded.")
        previous_undo = list(history.undo)
        previous_redo = list(history.redo)
        history.undo.append(command)
        history.redo.clear()
        try:
            self._write(command.library_id)
        except Exception:
            history.undo[:] = previous_undo
            history.redo[:] = previous_redo
            raise
        self._finish_pending(command.library_id)

    def commit_undo(self, command: SessionCommand) -> None:
        history = self.history(command.library_id)
        if not history.undo or history.undo[-1].command_id != command.command_id:
            raise ValueError("Undo command does not match the current session history.")
        previous_undo = list(history.undo)
        previous_redo = list(history.redo)
        history.undo.pop()
        history.redo.append(command)
        try:
            self._write(command.library_id)
        except Exception:
            history.undo[:] = previous_undo
            history.redo[:] = previous_redo
            raise
        self._finish_pending(command.library_id)

    def commit_redo(self, command: SessionCommand) -> None:
        history = self.history(command.library_id)
        if not history.redo or history.redo[-1].command_id != command.command_id:
            raise ValueError("Redo command does not match the current session history.")
        previous_undo = list(history.undo)
        previous_redo = list(history.redo)
        history.redo.pop()
        history.undo.append(command)
        try:
            self._write(command.library_id)
        except Exception:
            history.undo[:] = previous_undo
            history.redo[:] = previous_redo
            raise
        self._finish_pending(command.library_id)

    def pending_recovery(self) -> tuple[SessionRecoveryRecord, ...]:
        if not self.root.exists() or is_link_like(self.root):
            return ()
        records: list[SessionRecoveryRecord] = []
        try:
            folders = tuple(self.root.iterdir())
        except OSError:
            return ()
        for folder in folders:
            if is_link_like(folder) or not folder.is_dir() or not _SAFE_ID.fullmatch(folder.name):
                continue
            path = folder / "session.json"
            if is_link_like(path) or not path.is_file():
                continue
            try:
                if path.stat().st_size > _MAX_SESSION_BYTES:
                    continue
                with path.open("rb") as handle:
                    raw = handle.read(_MAX_SESSION_BYTES + 1)
                if len(raw) > _MAX_SESSION_BYTES:
                    continue
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    continue
                schema_version = int(payload.get("schema_version", 0))
                if schema_version < 0 or schema_version > 4:
                    continue
                redo_commands: list[SessionCommand] = []
                unresolved_commands: list[SessionCommand] = []
                if schema_version >= 3:
                    raw_ids = payload.get(
                        "undo_ids" if schema_version >= 4 else "command_ids", []
                    )
                    if not isinstance(raw_ids, list) or not all(
                        isinstance(item, str) and _SAFE_ID.fullmatch(item)
                        for item in raw_ids
                    ) or len(raw_ids) != len(set(raw_ids)):
                        continue
                    commands = [
                        self._read_command(folder.name, command_id)
                        for command_id in raw_ids
                    ]
                    raw_redo_ids = payload.get("redo_ids", []) if schema_version >= 4 else []
                    if not isinstance(raw_redo_ids, list) or not all(
                        isinstance(item, str) and _SAFE_ID.fullmatch(item)
                        for item in raw_redo_ids
                    ) or len(raw_redo_ids) != len(set(raw_redo_ids)):
                        continue
                    redo_commands = [
                        self._read_command(folder.name, command_id)
                        for command_id in raw_redo_ids
                    ]
                    pending_id = payload.get("pending_id")
                    pending = (
                        self._read_command(folder.name, str(pending_id))
                        if pending_id is not None
                        else None
                    )
                else:
                    raw_commands = payload.get("commands", [])
                    if not isinstance(raw_commands, list) or not all(
                        isinstance(item, dict) for item in raw_commands
                    ):
                        continue
                    commands = [command_from_dict(item) for item in raw_commands]
                    raw_pending = payload.get("pending")
                    if raw_pending is not None and not isinstance(raw_pending, dict):
                        continue
                    pending = command_from_dict(raw_pending) if raw_pending else None
                if commands and any(
                    command.library_id != folder.name or command.folder != commands[0].folder
                    for command in commands
                ):
                    continue
                if pending is not None:
                    candidate = pending
                    expected_folder = commands[0].folder if commands else candidate.folder
                    if candidate.library_id != folder.name or candidate.folder != expected_folder:
                        continue
                    action = str(payload.get("pending_action", "forward"))
                    state = self._command_disk_state(candidate)
                    if action == "forward" and state == "after":
                        commands.append(candidate)
                    elif action == "undo" and state == "before":
                        if commands and commands[-1].command_id == candidate.command_id:
                            redo_commands.append(commands.pop())
                    elif (
                        action == "redo"
                        and state == "after"
                        and (not commands or commands[-1].command_id != candidate.command_id)
                    ):
                        commands.append(candidate)
                        if redo_commands and redo_commands[-1].command_id == candidate.command_id:
                            redo_commands.pop()
                    elif state == "unknown":
                        unresolved_commands.append(candidate)
                all_commands = tuple(
                    dict.fromkeys((*commands, *redo_commands, *unresolved_commands))
                )
                if not all_commands and pending is None and schema_version == 4:
                    settled, orphan_commands = self._settled_empty_session(folder, payload)
                    if settled:
                        self.history(folder.name).persisted = True
                        self._finish_pending(folder.name)
                        continue
                    if orphan_commands:
                        all_commands = orphan_commands
                        unresolved_commands.extend(orphan_commands)
                if all_commands and any(
                    command.library_id != folder.name
                    or command.folder != all_commands[0].folder
                    for command in all_commands
                ):
                    continue
                if all_commands:
                    records.append(
                        SessionRecoveryRecord(
                            path.parent.name,
                            all_commands[0].folder,
                            tuple(commands),
                            all_commands,
                            tuple(unresolved_commands),
                        )
                    )
            except (OSError, RuntimeError, ValueError, RecursionError, KeyError, TypeError):
                continue
        return tuple(records)

    def _settled_empty_session(
        self, folder: Path, payload: dict[str, object]
    ) -> tuple[bool, tuple[SessionCommand, ...]]:
        expected_fields = {
            "schema_version", "accepted", "command_ids", "undo_ids", "redo_ids",
            "pending_id", "pending_action",
        }
        if (
            set(payload) != expected_fields
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] != 4
            or type(payload["accepted"]) is not bool
            or any(payload[key] != [] for key in ("command_ids", "undo_ids", "redo_ids"))
            or payload["pending_id"] is not None
            or payload["pending_action"] != ""
        ):
            return False, ()
        entries = tuple(folder.iterdir())
        if any(is_link_like(path) or path.name not in {"session.json", "commands"} for path in entries):
            return False, ()
        commands_folder = folder / "commands"
        if not commands_folder.exists():
            return False, ()
        if not commands_folder.is_dir():
            return False, ()
        paths = tuple(commands_folder.iterdir())
        if any(
            is_link_like(path) or not path.is_file() or path.suffix != ".json"
            or not _SAFE_ID.fullmatch(path.stem)
            for path in paths
        ):
            return False, ()
        commands = tuple(self._read_command(folder.name, path.stem) for path in paths)
        if not commands:
            return False, ()
        library_folder = commands[0].folder
        if any(command.folder != library_folder for command in commands):
            return False, ()
        recovery = library_folder / SESSION_RECOVERY_FOLDER
        if is_link_like(library_folder) or not library_folder.is_dir() or is_link_like(recovery):
            return False, commands
        if not recovery.exists():
            return True, ()
        if not recovery.is_dir():
            return False, commands
        owned_ids = {command.command_id for command in commands}
        for command_folder in recovery.iterdir():
            if (
                command_folder.name not in owned_ids
                or is_link_like(command_folder)
                or not command_folder.is_dir()
                or any(command_folder.iterdir())
            ):
                return False, commands
        return True, ()

    def _read_command(self, library_id: str, command_id: str) -> SessionCommand:
        path = self._command_path(library_id, command_id)
        if (
            is_link_like(path.parent)
            or is_link_like(path)
            or not path.is_file()
            or path.stat().st_size > _MAX_SESSION_BYTES
        ):
            raise ValueError("Session command data is unavailable or unsafe.")
        with path.open("rb") as handle:
            raw = handle.read(_MAX_SESSION_BYTES + 1)
        if len(raw) > _MAX_SESSION_BYTES:
            raise ValueError("Session command data is too large.")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Session command data is malformed.")
        command = command_from_dict(payload)
        if command.library_id != library_id or command.command_id != command_id:
            raise ValueError("Session command identity does not match its storage path.")
        return command

    @staticmethod
    def _command_disk_state(command: SessionCommand) -> str:
        if is_link_like(command.folder) or not command.folder.is_dir():
            return "unknown"
        if any(
            not validate_recovery_ancestors(
                recovery_path(command.folder, command.command_id, state.recovery_name),
                command.folder,
            )
            for state in command.presence
        ):
            return "unknown"
        before_ids = {track_id for track_id, _name in command.before_names}
        after_ids = {track_id for track_id, _name in command.after_names}
        presence = {item.track_id: item for item in command.presence}

        def names_match(
            pairs: tuple[tuple[str, str], ...], present_ids: set[str]
        ) -> bool:
            if not all(
                (command.folder / name).is_file()
                and not is_link_like(command.folder / name)
                for _, name in pairs
            ):
                return False
            for track_id in (before_ids | after_ids) - present_ids:
                state = presence.get(track_id)
                if state is None:
                    return False
                path = recovery_path(
                    command.folder, command.command_id, state.recovery_name
                )
                if not path.is_file() or is_link_like(path):
                    return False
                try:
                    if path.stat().st_size != state.size:
                        return False
                    if state.sha256:
                        from .integrity import file_sha256

                        if file_sha256(path).casefold() != state.sha256.casefold():
                            return False
                except OSError:
                    return False
            return True

        def playlist_matches(data: bytes | None) -> bool:
            if command.playlist_path is None:
                return True
            if data is None:
                return not command.playlist_path.exists()
            if is_link_like(command.playlist_path):
                return False
            try:
                return hashlib.sha256(command.playlist_path.read_bytes()).digest() == hashlib.sha256(data).digest()
            except OSError:
                return False

        after_matches = names_match(command.after_names, after_ids) and playlist_matches(command.after_playlist_bytes)
        before_matches = names_match(command.before_names, before_ids) and playlist_matches(command.before_playlist_bytes)
        if after_matches and not before_matches:
            return "after"
        if before_matches and not after_matches:
            return "before"
        if after_matches and before_matches:
            try:
                from .config_service import load_config

                record = next(
                    (item for item in load_config().libraries if item.library_id == command.library_id),
                    None,
                )
                if record is not None:
                    current = (record.storage_mode, record.setup_completed, record.canonical_playlist)
                    after = (
                        command.after_storage_mode,
                        command.after_setup_completed,
                        command.after_canonical_playlist,
                    )
                    before = (
                        command.before_storage_mode,
                        command.before_setup_completed,
                        command.before_canonical_playlist,
                    )
                    if current == after and current != before:
                        return "after"
                    if current == before and current != after:
                        return "before"
            except Exception:
                pass
            return "unknown"
        return "unknown"

    def keep_recovery(self, library_id: str) -> bool:
        if not _SAFE_ID.fullmatch(library_id):
            return False
        folder = self.root / library_id
        if self._finalize_presence(folder):
            self._remove_tree(folder)
            if not folder.exists():
                self._finish_pending(library_id)
                self.histories.pop(library_id, None)
                return True
        return False

    def accept_all(self) -> None:
        if self._recovery_scan_failed or not self.root.exists() or is_link_like(self.root):
            return
        try:
            children = tuple(self.root.iterdir())
        except OSError:
            return
        self.accept_libraries(tuple(child.name for child in children
            if child.name in self.histories and child.name not in self._protected_libraries))

    def acceptance_ids(self) -> tuple[str, ...]:
        if self._recovery_scan_failed:
            return ()
        return tuple(
            key for key, history in self.histories.items()
            if key not in self._protected_libraries
            and (history.persisted or history.undo or history.redo)
        )

    def accept_libraries(self, library_ids: tuple[str, ...], stop_requested=None, *, finalize: bool = True) -> tuple[str, ...]:
        library_ids = tuple(dict.fromkeys(library_ids))
        candidates: dict[Path, tuple[int, str]] = {}
        for library_id in library_ids:
            if stop_requested is not None and stop_requested():
                return ()
            if not _SAFE_ID.fullmatch(library_id) or not self._finalize_presence(self.root / library_id, candidates):
                return ()
        try:
            for path, (size, digest) in candidates.items():
                if stop_requested is not None and stop_requested():
                    return ()
                verify_content(path, size, digest)
            if stop_requested is not None and stop_requested():
                return ()
            if candidates and (recycle_files(tuple(candidates)) or any(path.exists() for path in candidates)):
                return ()
            if stop_requested is not None and stop_requested():
                return ()
        except (OSError, RuntimeError, ValueError):
            return ()
        if not finalize:
            return library_ids
        accepted = []
        for library_id in library_ids:
            if self.keep_recovery(library_id):
                accepted.append(library_id)
        return tuple(accepted)

    def _finalize_presence(self, session_folder: Path, collect: dict[Path, tuple[int, str]] | None = None) -> bool:


        commands_folder = session_folder / "commands"
        if any(is_link_like(path) for path in (self.root, session_folder, commands_folder)):
            return False
        try:
            if any(
                is_link_like(path) or path.name not in {"session.json", "commands"}
                for path in session_folder.iterdir()
            ):
                return False
        except OSError:
            return False
        if not commands_folder.is_dir():
            return not commands_folder.exists()
        commands: list[SessionCommand] = []
        try:
            paths = tuple(commands_folder.iterdir())
        except OSError:
            return False
        for path in paths:
            if is_link_like(path) or not path.is_file() or path.suffix != ".json":
                return False
            try:
                command = self._read_command(session_folder.name, path.stem)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, RecursionError):
                return False
            commands.append(command)
        storage_owners: dict[Path, set[str]] = {}
        for command in commands:
            storage_owners.setdefault(command.folder, set()).add(command.command_id)
        for library_folder, command_ids in storage_owners.items():
            root = library_folder / SESSION_RECOVERY_FOLDER
            if is_link_like(root):
                return False
            if root.exists():
                try:
                    if not root.is_dir() or any(
                        path.name not in command_ids or is_link_like(path) or not path.is_dir()
                        for path in root.iterdir()
                    ):
                        return False
                except OSError:
                    return False
        for command in commands:
            probe = recovery_path(command.folder, command.command_id, "ownership.wav")
            if not validate_recovery_ancestors(probe, command.folder):
                return False
            command_folder = probe.parent
            if command_folder.exists():
                expected_names = {state.recovery_name for state in command.presence}
                try:
                    if any(path.name not in expected_names for path in command_folder.iterdir()):
                        return False
                except OSError:
                    return False
        expected_paths = tuple(
            recovery_path(command.folder, command.command_id, state.recovery_name)
            for command in commands
            for state in command.presence
        )
        if any(
            not validate_recovery_ancestors(
                recovery_path(command.folder, command.command_id, state.recovery_name),
                command.folder,
            )
            for command in commands
            for state in command.presence
        ):
            return False
        if any(
            path.exists() and (not path.is_file() or is_link_like(path))
            for path in expected_paths
        ):
            return False
        recovery_files = tuple(path for path in expected_paths if path.exists())
        evidence: dict[Path, tuple[int, str]] = {}
        try:
            for command in commands:
                for state in command.presence:
                    path = recovery_path(command.folder, command.command_id, state.recovery_name)
                    expected = (state.size, state.sha256)
                    if path in evidence and evidence[path] != expected:
                        return False
                    if path.exists():
                        verify_content(path, *expected)
                        evidence[path] = expected
            if collect is not None:
                for path, expected in evidence.items():
                    if path in collect and collect[path] != expected:
                        return False
                collect.update(evidence)
                return True
            if recovery_files and (recycle_files(tuple(evidence)) or any(path.exists() for path in evidence)):
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        for command in commands:
            command_folder = (
                command.folder / SESSION_RECOVERY_FOLDER / command.command_id
            )
            try:
                command_folder.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                return False
        for library_folder in storage_owners:
            try:
                (library_folder / SESSION_RECOVERY_FOLDER).rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                return False
        return True

    @staticmethod
    def _remove_tree(folder: Path) -> None:
        if is_link_like(folder):
            return
        try:
            for item in folder.iterdir():
                if item.is_dir() and not is_link_like(item):
                    SessionManager._remove_tree(item)
                elif item.is_file() and not is_link_like(item):
                    item.unlink(missing_ok=True)
            folder.rmdir()
        except OSError:
            pass
