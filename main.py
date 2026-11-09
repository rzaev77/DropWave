"""DropWave — мгновенная передача файлов и текста между ПК и смартфоном по LAN.

Запуск из корня проекта:
    python main.py                 # порт выбирается автоматически (8000-8090)
    python main.py --port 8050     # предпочтительный порт
    python main.py --dir D:\\Share # своя папка хранения (по умолчанию <Downloads>/DropWave)
    python main.py --no-tray       # без сворачивания в трей

ПК и телефон должны быть в одной Wi-Fi/Ethernet сети. Интернет не требуется:
сервер работает локально, страница телефона отдаётся самим ПК.
"""

from __future__ import annotations

import argparse
import importlib.util
import signal
import sys

REQUIRED = {
    "PySide6": "PySide6",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
    "aiofiles": "aiofiles",
    "qrcode": "qrcode[pil]",
    "PIL": "pillow",
    "pyperclip": "pyperclip",
}


def _check_dependencies() -> list[str]:
    missing = []
    for module, package in REQUIRED.items():
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(package)
        except (ImportError, ValueError):
            missing.append(package)
    return missing


def main() -> int:
    # Консоль Windows может быть в cp1251 — не роняем приложение на печати
    # символов вне кодовой страницы (стрелки, эмодзи в логах сервера).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="DropWave",
        description="Передача файлов и текста между ПК и телефоном по локальной сети.",
    )
    parser.add_argument("--port", type=int, default=None,
                        help="предпочтительный порт (по умолчанию: свободный из 8000-8090)")
    parser.add_argument("--dir", type=str, default=None,
                        help="папка хранения файлов (по умолчанию: <Downloads>/DropWave)")
    parser.add_argument("--no-tray", action="store_true",
                        help="не сворачиваться в системный трей при закрытии окна")
    args = parser.parse_args()

    missing = _check_dependencies()
    if missing:
        print("Не установлены зависимости: " + ", ".join(missing))
        print("Установите их командой:  pip install -r requirements.txt")
        return 1

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from core.network import NoFreePortError, resolve_network
    from core.storage import StorageError, prepare_directory
    from ui.main_window import MainWindow
    from ui.theme import STYLESHEET, make_app_icon

    app = QApplication(sys.argv)
    app.setApplicationName("DropWave")
    app.setOrganizationName("DropWave")
    app.setWindowIcon(make_app_icon())
    app.setStyleSheet(STYLESHEET)
    # Окно прячется в трей — приложение не должно завершаться само.
    app.setQuitOnLastWindowClosed(False)

    # Ctrl+C в консоли должен корректно завершать Qt-приложение.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    sigint_timer = QTimer()
    sigint_timer.timeout.connect(lambda: None)  # будит Python для обработки SIGINT
    sigint_timer.start(200)

    try:
        directory = prepare_directory(args.dir)
    except StorageError as exc:
        QMessageBox.critical(None, "DropWave — нет доступа к папке", str(exc))
        return 1

    try:
        net = resolve_network(preferred_port=args.port)
    except NoFreePortError as exc:
        QMessageBox.critical(None, "DropWave — нет свободных портов", str(exc))
        return 1

    for warning in net.warnings:
        print(f"[warn] {warning}")
    print(f"Папка хранения: {directory}")

    window = MainWindow(net, directory, use_tray=not args.no_tray)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())