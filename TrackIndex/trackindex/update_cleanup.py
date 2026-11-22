from __future__ import annotations

import ctypes
import json
import math
import os
import re
import time
from ctypes import wintypes
from contextlib import contextmanager, suppress
from pathlib import Path

from .update_handoff import PRODUCT, _plain_path, read_state, state_lease, state_root, write_state

_TERMINAL = {'success', 'consumed', 'failed', 'aborted'}
_ACTIVE = {'arming', 'ready', 'committed', 'installing'}
_STAGING = re.compile(re.escape(PRODUCT.lower()) + r'-update-[0-9a-f]{32}$')


class PreparationLease:
    def __init__(self, staging: Path):
        self.path = _staging_path(str(staging), _plain_path(staging.parent))
        directory = _plain_path(staging.parent / '.update-preparation')
        directory.mkdir(exist_ok=True)
        self.stream = None
        stream = _plain_path(directory / (staging.name + '.lock')).open('a+b')
        try:
            if not stream.tell():
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            self.stream = stream
        except BaseException:
            stream.close()
            raise

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


@contextmanager
def _preparation_lease(staging: Path):
    lock = _plain_path(staging.parent / '.update-preparation' / (staging.name + '.lock'))
    lease = PreparationLease(staging) if lock.exists() else None
    try:
        yield
    finally:
        if lease is not None:
            lease.close()


def _processes(names: set[str]) -> list[tuple[int, str, str | None]]:
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
            ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            name = entry.szExeFile.casefold()
            if name in names or name.endswith('.tmp'):
                image = None
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if handle:
                    try:
                        buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            image = os.path.normcase(os.path.abspath(buffer.value))
                    finally:
                        kernel.CloseHandle(handle)
                result.append((int(entry.th32ProcessID), name, image))
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:
                    raise ctypes.WinError(ctypes.get_last_error())
                return result
    finally:
        kernel.CloseHandle(snapshot)


def _staging_path(value: object, storage: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError('Missing staging path')
    path = _plain_path(Path(value))
    if path.parent != storage or not _STAGING.fullmatch(path.name):
        raise ValueError('Staging is outside owned application storage')
    return path


def _active(data: dict, staging: Path, processes, *, parent: bool) -> bool:
    prefix = os.path.normcase(str(staging)) + os.sep
    setup = Path(str(data.get('setup', ''))).name.casefold()
    engine = Path(setup).with_suffix('.tmp').name if setup else ''
    parent_pid = data.get('parent_pid')
    bootloader_pid = data.get('bootloader_pid')
    for pid, name, image in processes:
        if not setup and name.endswith('.tmp'):
            return True
        if name == engine and engine:
            return True
        if image is None and name in {'updatehelper.exe', setup, engine}:
            return True
        if image is not None and image.startswith(prefix):
            return True
        if parent and pid in {parent_pid, bootloader_pid} and name == PRODUCT.casefold() + '.exe':
            return True
    return False


def _remove_staging(path: Path) -> bool:
    if not path.exists():
        return True
    directories, files = [], []
    pending = [path]
    while pending:
        directory = _plain_path(pending.pop())
        directories.append(directory)
        for entry in directory.iterdir():
            entry = _plain_path(entry)
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                files.append(entry)
            else:
                raise OSError('Unexpected staging entry')
            if len(files) + len(directories) + len(pending) > 512:
                raise OSError('Unexpected staging contents')
    for file in files:
        _plain_path(file).unlink(missing_ok=True)
    for directory in reversed(directories):
        _plain_path(directory).rmdir()
    return True


@contextmanager
def _ownership_lease(storage: Path, staging: Path):
    lock = _plain_path(storage / '.update-ownership' / (staging.name + '.lock'))
    if not lock.exists():
        yield
        return
    import msvcrt
    with lock.open('r+b') as stream:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _forget_ownership(storage: Path, staging: Path):
    if staging.exists():
        return
    for suffix in ('.json', '.tmp', '.lock'):
        try:
            _plain_path(storage / '.update-ownership' / (staging.name + suffix)).unlink(missing_ok=True)
        except (OSError, RuntimeError):
            pass
    try:
        _plain_path(storage / '.update-preparation' / (staging.name + '.lock')).unlink(missing_ok=True)
    except (OSError, RuntimeError):
        pass


def _cleanup_legacy_updates(storage: Path, cutoff: float, processes, result: dict) -> None:
    try:
        legacy = _plain_path(storage / 'updates')
    except RuntimeError:
        return
    if not legacy.is_dir():
        return
    setup_name = PRODUCT + 'Setup.exe'
    for staging in legacy.iterdir():
        if not _STAGING.fullmatch(staging.name.casefold()):
            continue
        try:
            staging = _plain_path(staging)
            if not staging.is_dir():
                continue
            with _ownership_lease(legacy, staging), _preparation_lease(staging):
                if staging.stat().st_mtime >= cutoff:
                    continue
                if _active({'setup': setup_name}, staging, processes, parent=False):
                    result['retry'] = True
                    continue
                for name in (setup_name, setup_name + '.tmp', 'UpdateHelper.exe'):
                    path = _plain_path(staging / name)
                    if path.is_file():
                        path.unlink()
                if not any(staging.iterdir()):
                    _plain_path(staging).rmdir()
                    result['removed'].append(str(staging))
            _forget_ownership(legacy, staging)
        except (OSError, RuntimeError):
            continue
    with suppress(OSError):
        _plain_path(legacy).rmdir()


def maintain_updates(storage_root: Path | None = None, *, orphans: bool = True, max_age_days: int = 1) -> dict:
    result = {'retry': False, 'removed': [], 'recovered': []}
    if os.name != 'nt':
        return result
    try:
        root = _plain_path(Path(storage_root) / 'update-state' if storage_root is not None else state_root())
        storage = _plain_path(root.parent)
        if not storage.is_dir():
            return result
        records = []
        names = {'updatehelper.exe', PRODUCT.casefold() + '.exe', PRODUCT.casefold() + 'setup.exe'}
        referenced = set()
        for path in root.glob('*.json'):
            if not re.fullmatch(r'[0-9a-f]{32}\.json', path.name):
                continue
            try:
                data = read_state(path)
                staging = _staging_path(data.get('staging'), storage)
                referenced.add(staging)
                setup = Path(str(data.get('setup', '')))
                if setup.parent != staging or setup.suffix.casefold() != '.exe':
                    continue
                names.update({setup.name.casefold(), setup.with_suffix('.tmp').name.casefold()})
                records.append((path, staging))
            except (OSError, ValueError, RuntimeError, TypeError):
                continue
        for staging in storage.iterdir() if orphans else ():
            if staging in referenced or not _STAGING.fullmatch(staging.name):
                continue
            try:
                for executable in _plain_path(staging).glob('*.exe'):
                    names.add(executable.name.casefold())
            except (OSError, RuntimeError):
                continue
        processes = _processes(names)
        now = time.time()
        cutoff = now - max_age_days * 86400
        for path, staging in records:
            try:
                with _ownership_lease(storage, staging), _preparation_lease(staging), state_lease(path, root=root):
                    data = read_state(path)
                    if _staging_path(data.get('staging'), storage) != staging:
                        continue
                    phase = data.get('state')
                    if phase not in _TERMINAL | _ACTIVE:
                        continue
                    if _active(data, staging, processes, parent=phase in _ACTIVE):
                        result['retry'] = True
                        continue
                    if phase in _ACTIVE:
                        created = float(data.get('created', now))
                        if not math.isfinite(created) or now - created < 30:
                            result['retry'] = True
                            continue
                        data.update(state='failed', finished=now, recovery_pending=True,
                            error='An earlier update was interrupted. Run the latest installer to finish updating before relying on the installed version.')
                        write_state(path, data)
                        result['recovered'].append(str(path))
                    if _remove_staging(staging):
                        result['removed'].append(str(staging))
                _forget_ownership(storage, staging)
            except (OSError, ValueError, RuntimeError, TypeError):
                result['retry'] = True
        for staging in storage.iterdir() if orphans else ():
            if staging in referenced or not _STAGING.fullmatch(staging.name):
                continue
            try:
                staging = _plain_path(staging)
                if not staging.is_dir():
                    continue
                with _ownership_lease(storage, staging), _preparation_lease(staging):
                    recent = staging.stat().st_mtime >= cutoff
                    ownership = _plain_path(storage / '.update-ownership' / (staging.name + '.json'))
                    if recent or ownership.exists():
                        if not ownership.is_file() or ownership.stat().st_size > 4096:
                            continue
                        state = json.loads(ownership.read_text(encoding='utf-8'))
                        if not isinstance(state, dict) or state.get('phase') not in {'preparing', 'prepared', 'discarded'}:
                            continue
                    if _active({}, staging, processes, parent=False):
                        result['retry'] = True
                        continue
                    if _remove_staging(staging):
                        result['removed'].append(str(staging))
                _forget_ownership(storage, staging)
            except (OSError, RuntimeError, ValueError):
                continue
        if orphans:
            _cleanup_legacy_updates(storage, cutoff, processes, result)
    except (OSError, RuntimeError):
        result['retry'] = True
    return result


def discard_staging(staging: Path, storage_root: Path | None = None) -> None:
    try:
        storage = _plain_path(storage_root if storage_root is not None else state_root().parent)
        staging = _staging_path(str(staging), storage)
        unreadable_record = False
        for path in (storage / 'update-state').glob('*.json'):
            if not re.fullmatch(r'[0-9a-f]{32}\.json', path.name):
                continue
            try:
                data = read_state(path)
            except (OSError, RuntimeError, ValueError):
                unreadable_record = True
                continue
            if data.get('staging') == str(staging):
                maintain_updates(storage, orphans=False)
                return
        with _ownership_lease(storage, staging), _preparation_lease(staging):
            # Handoff stages the helper before creating its transaction record.
            if unreadable_record and (staging / 'UpdateHelper.exe').exists():
                return
            names = {'updatehelper.exe'}
            names.update(path.name.casefold() for path in staging.glob('*.exe'))
            if not _active({}, staging, _processes(names), parent=False):
                _remove_staging(staging)
        _forget_ownership(storage, staging)
    except (OSError, RuntimeError, ValueError):
        pass


def pending_recovery() -> tuple[Path, dict] | None:
    try:
        for path in _plain_path(state_root()).glob('*.json'):
            try:
                data = read_state(path)
                if data.get('state') == 'failed' and data.get('recovery_pending') is True:
                    return path, data
            except (OSError, ValueError, RuntimeError):
                continue
    except (OSError, RuntimeError):
        pass
    return None


def acknowledge_recovery(path: Path) -> None:
    with state_lease(path):
        data = read_state(path)
        data['recovery_pending'] = False
        write_state(path, data)
