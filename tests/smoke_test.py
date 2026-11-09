"""Смоук-тест DropWave без реального дисплея.

Запуск из корня:  python tests/smoke_test.py

Проверяет:
  * определение IP и свободного порта;
  * запуск FastAPI в QThread, страницу телефона, /api/info;
  * загрузку большого файла чанками, уникальные имена, скачивание с
    Content-Disposition, защиту от path traversal, удаление;
  * WebSocket: hello, ping/pong, текст телефон→ПК и ПК→телефон;
  * GUI (offscreen): QR, слоты сигналов, эхо-защиту буфера обмена.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

import httpx
from PySide6.QtWidgets import QApplication

from core.network import NetworkInfo, find_free_port, get_local_ip, is_port_free
from core.server import ServerSignals, ServerThread
from core.storage import prepare_directory

PASSED: list[str] = []


def ok(name: str):
    PASSED.append(name)
    print(f"  [OK] {name}")


def wait_for(cond, timeout=15.0, app: QApplication | None = None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app:
            app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_network():
    print("== Сеть ==")
    ip = get_local_ip()
    assert ip and not ip.startswith("127."), f"подозрительный IP: {ip}"
    ok(f"LAN IP определён: {ip}")
    port = find_free_port()
    assert 8000 <= port <= 8090 and is_port_free(port)
    ok(f"Свободный порт найден: {port}")


def test_server(app: QApplication, directory: Path):
    print("== Сервер (QThread + FastAPI) ==")
    signals = ServerSignals()
    events = {"started": None, "texts": [], "files": [], "clients": []}
    signals.started.connect(lambda url: events.update(started=url))
    signals.text_received.connect(lambda t, s: events["texts"].append((t, s)))
    signals.file_received.connect(lambda p: events["files"].append(p))
    signals.clients_changed.connect(lambda c: events["clients"].append(c))

    port = find_free_port(port_range=(8900, 8920))
    thread = ServerThread(directory, "127.0.0.1", [port, port + 1], signals)
    thread.start()
    assert wait_for(lambda: events["started"] is not None, app=app), "сервер не стартовал"
    base = events["started"]
    ok(f"сервер запущен: {base}")

    with httpx.Client(base_url=base, timeout=30) as client:
        r = client.get("/")
        assert r.status_code == 200 and "DropWave" in r.text
        ok("GET / отдаёт мобильную страницу")

        r = client.get("/api/info")
        assert r.status_code == 200 and r.json()["port"] == port
        ok("GET /api/info")

        payload = os.urandom(3 * 1024 * 1024 + 777)  # 3 МБ + хвост
        name = "тест файл.bin"
        r = client.post("/upload", content=payload,
                        headers={"X-Filename": quote(name), "X-Device": "TestPhone"})
        assert r.status_code == 200 and r.json()["ok"], r.text
        saved = r.json()["file"]["name"]
        assert (directory / saved).read_bytes() == payload
        ok(f"POST /upload: 3 МБ сохранён как {saved}")

        r = client.post("/upload", content=b"duplicate",
                        headers={"X-Filename": quote(name)})
        second = r.json()["file"]["name"]
        assert second != saved and (directory / second).exists()
        assert (directory / saved).read_bytes() == payload  # оригинал не перезаписан
        ok(f"повторное имя → уникальное: {second}")

        r = client.post("/upload", content=(b"x" * 1024 for _ in range(64)))
        assert r.status_code == 200 and r.json()["file"]["size"] == 65536
        ok("chunked-загрузка без Content-Length")

        r = client.get("/api/files")
        names = [f["name"] for f in r.json()["files"]]
        assert saved in names and second in names
        ok("GET /api/files")

        r = client.get(f"/download/{quote(saved)}")
        assert r.status_code == 200 and r.content == payload
        cd = r.headers["content-disposition"]
        assert "attachment" in cd and "filename*=UTF-8''" in cd
        assert r.headers["content-length"] == str(len(payload))
        ok("GET /download: потоковая отдача + Content-Disposition")

        r = client.get("/download/..%2F..%2Fsecret.txt")
        assert r.status_code in (400, 404)
        ok("path traversal отклонён")

        r = client.delete(f"/api/files/{quote(second)}")
        assert r.status_code == 200 and not (directory / second).exists()
        ok("DELETE /api/files")

        r = client.post("/api/text", json={"text": "привет по HTTP", "device": "TestPhone"})
        assert r.status_code == 200
        ok("POST /api/text (fallback)")

    # -- WebSocket -----------------------------------------------------------
    async def ws_checks():
        from websockets.asyncio.client import connect

        async with connect(f"ws://127.0.0.1:{port}/ws?device=TestPhone") as ws:
            async def recv_type(t, timeout=8):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    import json
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                    if msg.get("type") == t:
                        return msg
                raise AssertionError(f"не дождались сообщения {t}")

            hello = await recv_type("hello")
            assert hello["id"] and isinstance(hello["files"], list)
            ok("WS: hello со списком файлов")

            await ws.send('{"type":"ping","ts":123}')
            pong = await recv_type("pong")
            assert pong["ts"] == 123
            ok("WS: ping → pong")

            await ws.send('{"type":"text","text":"привет с телефона"}')
            # сервер исключает отправителя из broadcast — проверяем Qt-сигнал
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                app.processEvents()
                if any(t == "привет с телефона" for t, _ in events["texts"]):
                    break
                await asyncio.sleep(0.05)
            assert any(t == "привет с телефона" for t, _ in events["texts"])
            ok("WS: текст телефон → ПК (сигнал GUI)")

            thread.send_text_to_clients("привет с ПК")
            msg = await recv_type("text")
            assert msg["text"] == "привет с ПК" and msg["source"] == "ПК"
            ok("WS: текст ПК → телефон (thread-safe)")

            await ws.send('{"type":"get_files"}')
            files_msg = await recv_type("files")
            assert isinstance(files_msg["files"], list)
            ok("WS: get_files")

    asyncio.run(ws_checks())
    app.processEvents()
    assert events["clients"], "GUI не получал clients_changed"
    ok("WS: подключение/отключение уведомляет GUI")

    thread.stop()
    assert wait_for(lambda: not thread.isRunning(), app=app), "поток не остановился"
    ok("мягкая остановка сервера")
    return events


def test_gui(app: QApplication, directory: Path):
    print("== GUI (offscreen) ==")
    from ui.main_window import MainWindow

    port = find_free_port(port_range=(8930, 8950))
    net = NetworkInfo(ip="127.0.0.1", port=port, url=f"http://127.0.0.1:{port}")
    window = MainWindow(net, directory, use_tray=False)
    window.show()
    assert wait_for(lambda: window._server_online, app=app), "GUI-сервер не стартовал"
    ok(f"MainWindow: сервер онлайн на {window.url}")

    assert not window.qr_label.pixmap().isNull()
    ok("QR-код отрисован")

    window._on_clients([{"id": "t1", "name": "TestPhone", "address": "192.168.1.50",
                         "since": time.time()}])
    assert window.devices_list.count() == 1
    window._on_file_received({"name": "фото.jpg", "size": 2048, "path": str(directory / "фото.jpg"),
                              "client": "TestPhone"})
    assert window.files_list.count() == 1
    window._on_progress({"id": "p1", "direction": "upload", "name": "фото.jpg", "loaded": 1024,
                         "total": 2048, "percent": 50.0, "speed": 1024, "eta": 1.0,
                         "state": "active"})
    assert window.progress.value() == 50
    window._on_progress({"id": "p1", "direction": "upload", "name": "фото.jpg", "loaded": 2048,
                         "total": 2048, "percent": 100, "speed": 1024, "eta": 0,
                         "state": "done"})
    ok("слоты: устройства, лог файлов, прогресс")

    # Эхо-защита буфера обмена
    sent: list[str] = []
    window.server.send_text_to_clients = lambda text: sent.append(text)
    window.auto_paste.setChecked(True)
    window._on_text_received("уникальный текст 12345", "TestPhone")
    assert QApplication.clipboard().text() == "уникальный текст 12345"
    window._poll_clipboard()
    assert not sent, f"рекурсия! буфер ушёл обратно: {sent}"
    ok("авто-вставка не создаёт рекурсивное эхо")

    QApplication.clipboard().setText("совсем новый текст 67890")
    window._poll_clipboard()
    assert sent == ["совсем новый текст 67890"]
    ok("новое копирование на ПК отправляется на телефон")

    window._copy_url()
    assert QApplication.clipboard().text() == window.url
    window._poll_clipboard()
    assert len(sent) == 1, "копирование ссылки не должно улетать на телефон"
    ok("копирование URL не триггерит синхронизацию")

    with httpx.Client(base_url=window.url, timeout=10) as client:
        assert client.get("/").status_code == 200
    ok("GUI-сервер отдаёт страницу телефона")

    window._shutdown()
    assert wait_for(lambda: not window.server.isRunning(), app=app)
    window.close()
    ok("GUI корректно завершён")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    with tempfile.TemporaryDirectory(prefix="dropwave_test_") as tmp:
        directory = prepare_directory(tmp)
        test_network()
        test_server(app, directory)
        test_gui(app, directory)
    print(f"\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ: {len(PASSED)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())