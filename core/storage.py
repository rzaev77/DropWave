"""Файловое хранилище DropWave: безопасные имена, уникальные пути, листинг.

Все файлы (полученные с телефона и расшаренные с ПК) живут в одной папке
``<Downloads>/DropWave``._received-файлы не перезаписывают существующие:
при конфликте имён добавляется суффикс « (1)», « (2)», …
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import uuid
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1 МБ — буфер чтения/записи при стриминге
MAX_FILENAME_LEN = 180

# Зарезервированные имена устройств Windows.
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


class StorageError(RuntimeError):
    """Ошибка подготовки папки хранения или доступа к файлам."""


def default_directory() -> Path:
    """Папка загрузок пользователя + подпапка DropWave (кроссплатформенно)."""
    home = Path.home()
    if os.name == "nt":
        # Известная папка «Загрузки» может быть перенаправлена (OneDrive и т.п.).
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                raw, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            downloads = Path(os.path.expandvars(raw))
            if downloads.is_absolute():
                return downloads / "DropWave"
        except Exception:
            pass
    return home / "Downloads" / "DropWave"


def prepare_directory(path: Path | str | None = None) -> Path:
    """Создать папку и проверить права записи. Запасной вариант — ./DropWave.

    Бросает StorageError, если ни одна из папок недоступна для записи.
    """
    candidates = [Path(path).expanduser() if path else default_directory()]
    fallback = Path.cwd() / "DropWave"
    if fallback not in candidates:
        candidates.append(fallback)
    errors: list[str] = []
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".write_probe_{uuid.uuid4().hex[:8]}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return directory.resolve()
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
    raise StorageError(
        "Нет прав записи ни в одну из папок:\n" + "\n".join(errors)
    )


def sanitize_filename(name: str) -> str:
    """Имя файла без путей, управляющих символов и запрещённых имён Windows."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if not name:
        name = "file"
    stem = name.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        name = f"_{name}"
    if len(name) > MAX_FILENAME_LEN:
        p = Path(name)
        suffix = p.suffix[:16]
        name = p.stem[: MAX_FILENAME_LEN - len(suffix)] + suffix
    return name


def resolve_safe(directory: Path, filename: str) -> Path:
    """Путь внутри directory; защита от path traversal (../, абсолютные пути)."""
    clean = sanitize_filename(filename)
    path = (directory / clean).resolve()
    if path.parent != directory.resolve():
        raise StorageError(f"Недопустимое имя файла: {filename!r}")
    return path


def unique_path(directory: Path, filename: str) -> Path:
    """Свободный путь: photo.png → photo (1).png → photo (2).png …"""
    clean = sanitize_filename(filename)
    candidate = directory / clean
    if not candidate.exists():
        return candidate
    p = Path(clean)
    stem, suffix = p.stem, p.suffix
    for i in range(1, 10_000):
        candidate = directory / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def commit_temp_file(tmp: Path, directory: Path, filename: str) -> Path:
    """Атомарно (насколько позволяет ФС) перенести временный файл в хранилище.

    os.rename на Windows отказывается перезаписывать существующий файл —
    при гонке двух одноимённых загрузок подбираем следующий суффикс.
    """
    target = unique_path(directory, filename)
    for _ in range(100):
        try:
            os.rename(tmp, target)
            return target
        except FileExistsError:
            target = unique_path(directory, target.name)
        except OSError:
            if target.exists():  # POSIX: гонка, файл успели создать
                target = unique_path(directory, target.name)
                continue
            raise
    raise StorageError(f"Не удалось подобрать уникальное имя для {filename!r}")


def file_info(path: Path) -> dict:
    """Метаданные файла для JSON/сигналов."""
    st = path.stat()
    return {
        "name": path.name,
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


def list_files(directory: Path, limit: int = 500) -> list[dict]:
    """Файлы папки (без служебных/временных), свежие сверху."""
    items: list[dict] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return items
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.name.startswith(".") or entry.name.endswith(".part"):
            continue
        try:
            items.append(file_info(entry))
        except OSError:
            continue  # файл удалили/заблокировали во время листинга
    items.sort(key=lambda f: f["mtime"], reverse=True)
    return items[:limit]


def format_size(num: float) -> str:
    """Человекочитаемый размер: 1.4 МБ, 980 КБ …"""
    num = float(num)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(num) < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(num)} Б"
            return f"{num:.1f} {unit}".replace(".", ",")
        num /= 1024
    return f"{num:.1f} ТБ"


def format_speed(bytes_per_sec: float) -> str:
    return f"{format_size(bytes_per_sec)}/с"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds in (float("inf"), float("-inf")):
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60:02d} с"
    return f"{seconds // 3600} ч {(seconds % 3600) // 60:02d} мин"


def new_temp_path(directory: Path) -> Path:
    """Временный файл загрузки (скрытый, с суффиксом .part)."""
    return directory / f".{uuid.uuid4().hex}.part"