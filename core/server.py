"""FastAPI-бэкенд DropWave, работающий в отдельном QThread.

Архитектура:
  * ``ServerThread`` — QThread с собственным asyncio-loop и uvicorn-сервером.
    Общение с GUI только через Qt-сигналы ``ServerSignals`` (queued-соединения,
    поэтому emit из любого потока безопасен).
  * ``ConnectionManager`` — реестр WebSocket-клиентов, broadcast, пинги.
  * Файлы принимаются сырым потоком (``request.stream()``) с буфером 1 МБ и
    пишутся через aiofiles во временный ``.part``-файл, затем атомарно
    переименовываются в уникальное имя (без перезаписи существующих).

Протокол WebSocket (JSON):
  сервер → клиент: hello, files, file_added, file_removed, progress, text,
                   ping, clients
  клиент → сервер: text, ping, pong, get_files
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import aiofiles
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from PySide6.QtCore import QObject, QThread, Signal
from starlette.requests import ClientDisconnect

from core import __version__
from core.storage import (
    CHUNK_SIZE,
    StorageError,
    file_info,
    list_files,
    resolve_safe,
    sanitize_filename,
    unique_path,
)

PROGRESS_INTERVAL = 0.15   # сек, троттлинг progress-сообщений
PING_INTERVAL = 20.0       # сек, серверный пинг
CLIENT_TIMEOUT = 65.0      # сек, без ответа клиент считается мёртвым
MAX_TEXT_LEN = 200_000     # символов, защита от гигантских сообщений
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# ---------------------------------------------------------------------------
# Сигналы для GUI
# ---------------------------------------------------------------------------

class ServerSignals(QObject):
    """Мост server-thread → GUI-thread. Все сигналы несут простые типы."""

    started = Signal(str)             # url сервера
    stopped = Signal()
    error = Signal(str)               # фатальная ошибка запуска
    clients_changed = Signal(list)    # [{id,name,address,since}, ...]
    file_received = Signal(dict)      # {name,size,path,client} — приём с телефона
    files_changed = Signal()          # общая папка изменилась (для drag&drop)
    text_received = Signal(str, str)  # (текст, источник)
    progress = Signal(dict)           # словарь прогресса передачи
    log = Signal(str)                 # строка для статус-лога GUI


# ---------------------------------------------------------------------------
# Прогресс передач
# ---------------------------------------------------------------------------

class TransferMeter:
    """Счётчик скорости с экспоненциальным сглаживанием."""

    def __init__(self, total: int = 0):
        self.total = total
        self.loaded = 0
        self._start = time.monotonic()
        self._win_start = self._start
        self._win_bytes = 0
        self.speed = 0.0  # байт/с

    def advance(self, nbytes: int) -> None:
        self.loaded += nbytes
        self._win_bytes += nbytes
        now = time.monotonic()
        dt = now - self._win_start
        if dt >= 0.5:
            instant = self._win_bytes / dt
            self.speed = instant if self.speed == 0 else 0.6 * self.speed + 0.4 * instant
            self._win_start, self._win_bytes = now, 0

    def payload(self, tid: str, direction: str, name: str, state: str = "active",
                client: str = "") -> dict[str, Any]:
        elapsed = max(time.monotonic() - self._start, 1e-6)
        speed = self.speed or (self.loaded / elapsed)
        percent = 100.0 if state == "done" else (
            min(99.9, self.loaded * 100.0 / self.total) if self.total > 0 else 0.0
        )
        eta = None
        if state == "active" and self.total > 0 and speed > 0:
            eta = max(0.0, (self.total - self.loaded) / speed)
        return {
            "type": "progress", "id": tid, "direction": direction, "name": name,
            "loaded": self.loaded, "total": self.total, "percent": round(percent, 1),
            "speed": round(speed, 1), "eta": eta, "state": state, "client": client,
        }


# ---------------------------------------------------------------------------
# WebSocket-хаб
# ---------------------------------------------------------------------------

@dataclass
class Client:
    id: str
    ws: WebSocket
    name: str
    address: str
    since: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.monotonic)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "address": self.address, "since": self.since}


class ConnectionManager:
    """Реестр WS-клиентов. Все методы вызываются только из loop сервера."""

    def __init__(self, signals: ServerSignals):
        self.signals = signals
        self.clients: dict[str, Client] = {}

    async def connect(self, ws: WebSocket, name: str, address: str) -> Client:
        client = Client(id=uuid.uuid4().hex[:12], ws=ws, name=name[:60] or "Устройство",
                        address=address)
        self.clients[client.id] = client
        await self._notify_clients()
        self.signals.log.emit(f"Подключено: {client.name} ({address})")
        return client

    async def disconnect(self, client_id: str) -> None:
        client = self.clients.pop(client_id, None)
        if client is None:
            return
        try:
            await client.ws.close()
        except Exception:
            pass
        await self._notify_clients()
        self.signals.log.emit(f"Отключено: {client.name}")

    async def _notify_clients(self) -> None:
        snapshot = [c.public() for c in self.clients.values()]
        self.signals.clients_changed.emit(snapshot)
        await self.broadcast({"type": "clients", "clients": snapshot})

    async def send(self, client_id: str, payload: dict) -> None:
        client = self.clients.get(client_id)
        if client is None:
            return
        try:
            await client.ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            await self.disconnect(client_id)

    async def broadcast(self, payload: dict, exclude: str | None = None) -> None:
        if not self.clients:
            return
        data = json.dumps(payload, ensure_ascii=False)
        dead: list[str] = []
        for cid, client in list(self.clients.items()):
            if cid == exclude:
                continue
            try:
                await client.ws.send_text(data)
            except Exception:
                dead.append(cid)
        for cid in dead:
            await self.disconnect(cid)

    async def close_all(self) -> None:
        for cid in list(self.clients):
            await self.disconnect(cid)


# ---------------------------------------------------------------------------
# Контекст приложения
# ---------------------------------------------------------------------------

@dataclass
class ServerContext:
    directory: Path
    signals: ServerSignals
    host_ip: str
    port: int
    manager: ConnectionManager | None = None
    started_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.manager is None:
            self.manager = ConnectionManager(self.signals)


def _commit_file(tmp: Path, directory: Path, name: str) -> Path:
    """Перенести готовый .part в хранилище без перезаписи (гонки безопасны).

    os.link атомарно отказывает, если имя занято (NTFS/ext4); на ФС без
    хардлинков — fallback на rename c повторным подбором имени.
    """
    for _ in range(1000):
        candidate = unique_path(directory, name)
        try:
            os.link(tmp, candidate)
            tmp.unlink(missing_ok=True)
            return candidate
        except FileExistsError:
            continue
        except OSError:
            try:
                if candidate.exists():
                    continue
                tmp.rename(candidate)
                return candidate
            except FileExistsError:
                continue
    raise StorageError(f"Не удалось подобрать уникальное имя для {name}")


def create_app(ctx: ServerContext) -> FastAPI:
    """Фабрика FastAPI-приложения (отдельный экземпляр на каждую попытку порта)."""
    signals = ctx.signals
    manager: ConnectionManager = ctx.manager
    directory = ctx.directory

    # -- lifespan: пинги и контроль протухших клиентов -----------------------
    async def ping_loop():
        while True:
            await asyncio.sleep(PING_INTERVAL)
            now = time.monotonic()
            stale = [cid for cid, c in manager.clients.items()
                     if now - c.last_seen > CLIENT_TIMEOUT]
            for cid in stale:
                await manager.disconnect(cid)
            await manager.broadcast({"type": "ping", "ts": time.time()})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ping_task = asyncio.create_task(ping_loop())
        signals.started.emit(f"http://{ctx.host_ip}:{ctx.port}")
        signals.log.emit(f"Сервер запущен: http://{ctx.host_ip}:{ctx.port} → {directory}")
        try:
            yield
        finally:
            ping_task.cancel()
            await manager.close_all()

    app = FastAPI(title="DropWave", version=__version__, docs_url=None,
                  redoc_url=None, lifespan=lifespan)

    # -- страницы и справочники ----------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        index_path = WEB_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(500, "web/index.html не найден")
        return FileResponse(index_path, media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return JSONResponse({}, status_code=204)

    @app.get("/api/info")
    async def info():
        return {
            "app": "DropWave", "version": __version__,
            "ip": ctx.host_ip, "port": ctx.port,
            "directory": str(directory),
            "clients": len(manager.clients),
            "uptime": round(time.time() - ctx.started_at),
        }

    @app.get("/api/files")
    async def api_files():
        return {"files": list_files(directory)}

    @app.delete("/api/files/{filename}")
    async def api_delete(filename: str):
        try:
            path = resolve_safe(directory, filename)
        except StorageError:
            raise HTTPException(400, "Некорректное имя файла")
        if not path.is_file():
            raise HTTPException(404, "Файл не найден")
        try:
            path.unlink()
        except OSError as exc:
            raise HTTPException(500, f"Не удалось удалить: {exc}")
        await manager.broadcast({"type": "file_removed", "name": path.name})
        signals.files_changed.emit()
        return {"ok": True, "name": path.name}

    # -- текст (HTTP fallback, если WebSocket недоступен) ---------------------
    async def _deliver_text(text: str, source: str, exclude: str | None = None):
        text = text[:MAX_TEXT_LEN]
        if not text.strip():
            return
        await manager.broadcast(
            {"type": "text", "text": text, "source": source, "ts": time.time()},
            exclude=exclude,
        )
        signals.text_received.emit(text, source)

    @app.post("/api/text")
    async def api_text(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Ожидается JSON {\"text\": ...}")
        text = str(body.get("text", ""))
        if not text.strip():
            raise HTTPException(400, "Пустой текст")
        await _deliver_text(text, str(body.get("device", "Телефон"))[:60])
        return {"ok": True}

    # -- загрузка: сырой поток, буфер 1 МБ ------------------------------------
    @app.post("/upload")
    async def upload(request: Request):
        raw_name = request.headers.get("x-filename") or request.query_params.get("filename", "")
        name = sanitize_filename(unquote(raw_name) or "file.bin")
        try:
            total = int(request.headers.get("content-length") or 0)
        except ValueError:
            total = 0
        client_addr = request.client.host if request.client else "?"
        client_name = unquote(request.headers.get("x-device", ""))[:60] or client_addr
        tid = uuid.uuid4().hex[:12]
        meter = TransferMeter(total)
        tmp = directory / f".upload-{tid}.part"
        last_emit = 0.0

        async def emit(state: str = "active"):
            await manager.broadcast(meter.payload(tid, "upload", name, state, client_name))
            signals.progress.emit(meter.payload(tid, "upload", name, state, client_name))

        try:
            async with aiofiles.open(tmp, "wb") as out:
                buf = bytearray()
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    meter.advance(len(chunk))
                    if len(buf) >= CHUNK_SIZE:
                        await out.write(bytes(buf))
                        buf.clear()
                    now = time.monotonic()
                    if now - last_emit >= PROGRESS_INTERVAL:
                        last_emit = now
                        await emit()
                if buf:
                    await out.write(bytes(buf))
            final = await asyncio.to_thread(_commit_file, tmp, directory, name)
        except ClientDisconnect:
            tmp.unlink(missing_ok=True)
            await emit("cancelled")
            signals.log.emit(f"Загрузка прервана: {name}")
            return JSONResponse({"ok": False, "error": "client disconnected"}, status_code=499)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            await emit("error")
            signals.log.emit(f"Ошибка записи {name}: {exc}")
            raise HTTPException(507, f"Ошибка записи файла: {exc}")

        info = file_info(final)
        await emit("done")
        await manager.broadcast({"type": "file_added", "file": info})
        payload = {**info, "path": str(final), "client": client_name}
        signals.file_received.emit(payload)
        signals.log.emit(f"Получен файл: {final.name} ({info['size']} Б) от {client_name}")
        return {"ok": True, "file": info}

    # -- отдача: потоковое чтение с прогрессом --------------------------------
    @app.get("/download/{filename}")
    async def download(filename: str, request: Request):
        try:
            path = resolve_safe(directory, filename)
        except StorageError:
            raise HTTPException(400, "Некорректное имя файла")
        if not path.is_file():
            raise HTTPException(404, "Файл не найден")
        size = path.stat().st_size
        tid = uuid.uuid4().hex[:12]
        meter = TransferMeter(size)
        client_addr = request.client.host if request.client else "?"
        ascii_name = path.name.encode("ascii", "ignore").decode() or "download"
        headers = {
            "Content-Disposition":
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(path.name)}',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }

        async def stream():
            last_emit = 0.0
            try:
                async with aiofiles.open(path, "rb") as f:
                    while True:
                        chunk = await f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        meter.advance(len(chunk))
                        yield chunk
                        now = time.monotonic()
                        if now - last_emit >= PROGRESS_INTERVAL:
                            last_emit = now
                            await manager.broadcast(
                                meter.payload(tid, "download", path.name, "active", client_addr))
                await manager.broadcast(
                    meter.payload(tid, "download", path.name, "done", client_addr))
                signals.progress.emit(meter.payload(tid, "download", path.name, "done", client_addr))
            except (ClientDisconnect, asyncio.CancelledError):
                await manager.broadcast(
                    meter.payload(tid, "download", path.name, "cancelled", client_addr))
                raise

        return StreamingResponse(stream(), media_type="application/octet-stream", headers=headers)

    # -- WebSocket -------------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, device: str = ""):
        await ws.accept()
        address = ws.client.host if ws.client else "?"
        client = await manager.connect(ws, unquote(device), address)
        try:
            await manager.send(client.id, {
                "type": "hello", "id": client.id, "version": __version__,
                "files": list_files(directory),
                "server": {"ip": ctx.host_ip, "port": ctx.port},
            })
            while True:
                raw = await ws.receive_text()
                client.last_seen = time.monotonic()
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype == "pong":
                    continue
                if mtype == "ping":
                    await manager.send(client.id, {"type": "pong", "ts": msg.get("ts")})
                elif mtype == "text":
                    await _deliver_text(str(msg.get("text", "")), client.name, exclude=client.id)
                elif mtype == "get_files":
                    await manager.send(client.id, {"type": "files", "files": list_files(directory)})
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # обрыв сети, таймаут и т.п.
            signals.log.emit(f"WS-ошибка {client.name}: {exc}")
        finally:
            await manager.disconnect(client.id)

    return app


# ---------------------------------------------------------------------------
# QThread с asyncio-loop
# ---------------------------------------------------------------------------

class ServerThread(QThread):
    """Запускает uvicorn в собственном event-loop; перебирает свободные порты.

    Все обращения из GUI-потока — только через ``call_soon_threadsafe`` /
    ``run_coroutine_threadsafe`` или Qt-сигналы.
    """

    def __init__(self, directory: Path, host_ip: str, ports: list[int],
                 signals: ServerSignals, parent: QObject | None = None):
        super().__init__(parent)
        self.directory = directory
        self.host_ip = host_ip
        self.ports = ports
        self.signals = signals
        self.actual_port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._ctx: ServerContext | None = None

    # -- вызовы из GUI-потока -------------------------------------------------
    def send_text_to_clients(self, text: str) -> None:
        """Разослать текст с ПК всем WS-клиентам (буфер обмена desktop → phone)."""
        text = text[:MAX_TEXT_LEN]
        if not text.strip() or self._loop is None or self._ctx is None:
            return
        ctx = self._ctx

        async def _send():
            await ctx.manager.broadcast(
                {"type": "text", "text": text, "source": "ПК", "ts": time.time()})

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except RuntimeError:
            pass  # loop уже остановлен

    def notify_files_changed(self) -> None:
        """Сообщить клиентам, что папка изменилась (drag&drop на окно ПК)."""
        if self._loop is None or self._ctx is None:
            return
        ctx = self._ctx

        async def _notify():
            await ctx.manager.broadcast({"type": "files", "files": list_files(ctx.directory)})

        try:
            asyncio.run_coroutine_threadsafe(_notify(), self._loop)
        except RuntimeError:
            pass

    def stop(self) -> None:
        """Мягкая остановка: should_exit → uvicorn закрывает lifespan и сокеты."""
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(setattr, self._server, "should_exit", True)

    # -- тело потока -----------------------------------------------------------
    def run(self):  # noqa: C901 — линейный сценарий запуска
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:
            self.signals.error.emit(f"Сбой сервера: {exc}")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None
            self.signals.stopped.emit()

    async def _serve(self):
        last_error = ""
        for port in self.ports:
            # Привязываем сокет сами ДО запуска uvicorn: uvicorn выполняет
            # lifespan-startup раньше bind и при ошибке вызывает sys.exit,
            # поэтому доверенный retry по портам возможен только так.
            # Без SO_REUSEADDR — на Windows он позволил бы «угнать» занятый порт.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError as exc:
                sock.close()
                last_error = f"порт {port}: {exc}"
                self.signals.log.emit(f"Не удалось занять {last_error}, пробую следующий…")
                continue
            ctx = ServerContext(directory=self.directory, signals=self.signals,
                                host_ip=self.host_ip, port=port)
            self._ctx = ctx
            config = uvicorn.Config(
                create_app(ctx), host="0.0.0.0", port=port,
                log_level="warning", access_log=False, lifespan="on",
                ws_max_size=4 * 1024 * 1024, timeout_keep_alive=30,
            )
            server = uvicorn.Server(config)
            self._server = server
            self.actual_port = port
            try:
                await server.serve(sockets=[sock])
            except SystemExit:
                # uvicorn так сигналит о фатальной ошибке startup — не роняем
                # поток, пробуем следующий порт.
                last_error = f"порт {port}: ошибка запуска сервера"
                continue
            finally:
                sock.close()
            if server.started:
                return  # штатная остановка после успешного старта
            last_error = f"порт {port}: сервер не стартовал"
        self.signals.error.emit(
            f"Не удалось запустить сервер ({last_error}). "
            "Все порты диапазона заняты — закройте другие приложения "
            "или задайте порт: python main.py --port 8100"
        )


# ---------------------------------------------------------------------------
# Автономный запуск (отладка без GUI):  python -m core.server [--port 8000]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    from core.network import DEFAULT_PORT_RANGE, get_local_ip, NoLanAddressError
    from core.storage import prepare_directory

    parser = argparse.ArgumentParser(description="DropWave server (headless)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--dir", type=str, default=None)
    args = parser.parse_args()

    try:
        ip = get_local_ip()
    except NoLanAddressError:
        ip = "127.0.0.1"
    directory = prepare_directory(args.dir)
    ports = ([args.port] if args.port else []) + list(
        range(DEFAULT_PORT_RANGE[0], DEFAULT_PORT_RANGE[1] + 1))
    ports = list(dict.fromkeys(ports))

    sig = ServerSignals()
    sig.started.connect(lambda url: print(f"[started] {url}"))
    sig.log.connect(lambda line: print(f"[log] {line}"))
    sig.error.connect(lambda msg: print(f"[error] {msg}"))
    sig.text_received.connect(lambda text, src: print(f"[text] {src}: {text[:80]}"))
    sig.file_received.connect(lambda p: print(f"[file] {p['name']} ({p['size']} Б)"))

    thread = ServerThread(directory, ip, ports, sig)
    thread.start()
    print(f"Папка: {directory}\nCtrl+C для остановки.")
    try:
        while thread.isRunning():
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        thread.stop()
        thread.wait(5000)