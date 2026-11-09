"""Главное окно DropWave (PySide6, тёмный нео-брутализм).

Возможности:
  * QR-код и кликабельный URL для подключения телефона (клик = копирование);
  * индикатор подключённых устройств (онлайн/оффлайн);
  * живой прогресс передач (скорость, оставшееся время);
  * лог полученных файлов + «Показать в папке»;
  * синхронизация буфера обмена с защитой от рекурсивного эха;
  * сворачивание в системный трей при закрытии;
  * drag&drop файлов в окно → они появляются в списке на телефоне.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pyperclip
from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QProgressBar, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from core.network import DEFAULT_PORT_RANGE, NetworkInfo
from core.server import MAX_TEXT_LEN, ServerSignals, ServerThread
from core.storage import file_info, format_eta, format_size, format_speed, unique_path
from ui.theme import (
    ACCENT, ACCENT_INK, MUTED, OK, hard_shadow, make_app_icon, make_qr_pixmap,
)

CLIPBOARD_POLL_MS = 700
MAX_LOG_ITEMS = 200


class FileImportWorker(QThread):
    """Копирование перетащенных в окно файлов в общую папку (не блокирует GUI)."""

    imported = Signal(dict)
    failed = Signal(str)
    finished_count = Signal(int)

    def __init__(self, files: list[Path], directory: Path, parent=None):
        super().__init__(parent)
        self.files = files
        self.directory = directory

    def run(self):
        count = 0
        for src in self.files:
            try:
                dst = unique_path(self.directory, src.name)
                shutil.copy2(src, dst)
                self.imported.emit(file_info(dst))
                count += 1
            except OSError as exc:
                self.failed.emit(f"{src.name}: {exc}")
        self.finished_count.emit(count)


class MainWindow(QMainWindow):
    def __init__(self, net: NetworkInfo, directory: Path, use_tray: bool = True):
        super().__init__()
        self.net = net
        self.directory = directory
        self.url = f"http://{net.ip}:{net.port}"
        self._server_online = False
        self._clients: list[dict] = []
        self._transfers: dict[str, dict] = {}
        self._import_worker: FileImportWorker | None = None
        self._force_quit = False
        self._tray_hint_shown = False
        self._shutdown_done = False

        # Защита от рекурсии буфера обмена:
        #   _last_clip_text   — что монитор видел последним в буфере;
        #   _last_synced_text — последний текст, отправленный/полученный нами.
        self._last_clip_text = QApplication.clipboard().text() or ""
        self._last_synced_text = ""

        self.setWindowTitle("DropWave — файлы и текст по LAN")
        self.setWindowIcon(make_app_icon())
        self.resize(1060, 700)
        self.setMinimumSize(940, 620)
        self.setAcceptDrops(True)

        self.signals = ServerSignals()
        ports = [net.port] + [
            p for p in range(DEFAULT_PORT_RANGE[0], DEFAULT_PORT_RANGE[1] + 1)
            if p != net.port
        ]
        self.server = ServerThread(directory, net.ip, ports, self.signals, self)

        self._build_ui()
        self._connect_signals()
        self._setup_tray(use_tray)

        self._clip_timer = QTimer(self)
        self._clip_timer.setInterval(CLIPBOARD_POLL_MS)
        self._clip_timer.timeout.connect(self._poll_clipboard)

        self.server.start()

    # ------------------------------------------------------------------ UI
    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setProperty("card", True)
        hard_shadow(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(10)
        label = QLabel(title)
        label.setProperty("role", "cardTitle")
        lay.addWidget(label)
        return frame, lay

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 24, 12)
        root.setSpacing(14)
        self.setCentralWidget(central)

        # -- шапка ---------------------------------------------------------
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("DROPWAVE")
        title.setProperty("role", "title")
        subtitle = QLabel("ФАЙЛЫ И ТЕКСТ · ЛОКАЛЬНАЯ СЕТЬ · БЕЗ ИНТЕРНЕТА")
        subtitle.setProperty("role", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.status_chip = QLabel("ЗАПУСК…")
        self.status_chip.setProperty("role", "chip")
        self.status_chip.setAlignment(Qt.AlignCenter)
        header.addWidget(self.status_chip, 0, Qt.AlignTop)
        root.addLayout(header)

        # -- сетка карточек --------------------------------------------------
        grid = QGridLayout()
        grid.setSpacing(14)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(3, 1)
        root.addLayout(grid, 1)

        # Левая колонка: подключение (QR + URL)
        connect_card, clay = self._card("ПОДКЛЮЧЕНИЕ ТЕЛЕФОНА")
        connect_card.setFixedWidth(330)
        grid.addWidget(connect_card, 0, 0, 4, 1)

        qr_box = QFrame()
        qr_box.setStyleSheet("background:#ffffff; border:3px solid #000000;")
        qr_box.setFixedSize(264, 264)
        qr_lay = QVBoxLayout(qr_box)
        qr_lay.setContentsMargins(14, 14, 14, 14)
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setStyleSheet("background:#ffffff; border:none;")
        self.qr_label.setPixmap(make_qr_pixmap(self.url, 232))
        qr_lay.addWidget(self.qr_label)
        clay.addWidget(qr_box, 0, Qt.AlignHCenter)

        self.url_label = QLabel()
        self.url_label.setProperty("role", "url")
        self.url_label.setTextFormat(Qt.RichText)
        self.url_label.setOpenExternalLinks(False)
        self.url_label.setAlignment(Qt.AlignCenter)
        self.url_label.setToolTip("Нажмите, чтобы скопировать ссылку")
        self.url_label.setCursor(Qt.PointingHandCursor)
        self._set_url_text(self.url)
        self.url_label.linkActivated.connect(lambda _: self._copy_url())
        clay.addWidget(self.url_label)

        btn_row = QHBoxLayout()
        self.copy_btn = QPushButton("КОПИРОВАТЬ ССЫЛКУ")
        self.copy_btn.clicked.connect(self._copy_url)
        self.folder_btn = QPushButton("ОТКРЫТЬ ПАПКУ")
        self.folder_btn.setProperty("variant", "ghost")
        self.folder_btn.clicked.connect(self._open_folder)
        btn_row.addWidget(self.copy_btn, 1)
        btn_row.addWidget(self.folder_btn, 1)
        clay.addLayout(btn_row)

        hint = QLabel(
            "Наведите камеру телефона на QR-код.\n"
            "ПК и телефон должны быть в одной Wi-Fi сети."
        )
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignCenter)
        clay.addWidget(hint)

        if self.net.warnings:
            warn = QLabel("\n".join(f"⚠ {w}" for w in self.net.warnings))
            warn.setProperty("role", "muted")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            clay.addWidget(warn)
        clay.addStretch(1)

        # Правая колонка
        devices_card, dlay = self._card("УСТРОЙСТВА")
        dhead = QHBoxLayout()
        self.devices_count = QLabel("0 ОНЛАЙН")
        self.devices_count.setProperty("role", "chip")
        dhead.addStretch(1)
        dhead.addWidget(self.devices_count)
        dlay.addLayout(dhead)
        self.devices_list = QListWidget()
        self.devices_list.setFixedHeight(86)
        self.devices_list.setToolTip("Устройства, подключённые к серверу по WebSocket")
        dlay.addWidget(self.devices_list)
        grid.addWidget(devices_card, 0, 1)

        transfer_card, tlay = self._card("ПЕРЕДАЧА")
        self.transfer_name = QLabel("Ожидание передач…")
        self.transfer_name.setProperty("role", "big")
        self.transfer_name.setStyleSheet("font-size:15px;")
        self.transfer_stats = QLabel("")
        self.transfer_stats.setProperty("role", "muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        tlay.addWidget(self.transfer_name)
        tlay.addWidget(self.transfer_stats)
        tlay.addWidget(self.progress)
        grid.addWidget(transfer_card, 1, 1)

        clip_card, cclay = self._card("БУФЕР ОБМЕНА")
        self.auto_paste = QCheckBox("Авто-вставка текста в буфер обмена Windows")
        self.auto_paste.setChecked(True)
        self.auto_paste.setToolTip(
            "Текст с телефона сразу попадает в буфер обмена ПК.\n"
            "Эхо-защита не даёт тексту зациклиться между устройствами."
        )
        cclay.addWidget(self.auto_paste)
        self.clip_preview = QLabel("Текст ещё не синхронизировался")
        self.clip_preview.setProperty("role", "muted")
        self.clip_preview.setWordWrap(True)
        self.clip_preview.setMaximumHeight(40)
        cclay.addWidget(self.clip_preview)
        self.send_clip_btn = QPushButton("ОТПРАВИТЬ БУФЕР → ТЕЛЕФОН")
        self.send_clip_btn.setProperty("variant", "ghost")
        self.send_clip_btn.clicked.connect(self._send_clipboard_now)
        cclay.addWidget(self.send_clip_btn)
        clip_hint = QLabel("Текст, скопированный на ПК, автоматически улетает на подключённые устройства.")
        clip_hint.setProperty("role", "muted")
        clip_hint.setWordWrap(True)
        clip_hint.setStyleSheet("font-size:11px;")
        cclay.addWidget(clip_hint)
        grid.addWidget(clip_card, 2, 1)

        files_card, flay = self._card("ПОЛУЧЕННЫЕ ФАЙЛЫ")
        fhead = QHBoxLayout()
        fhead.addStretch(1)
        self.reveal_btn = QPushButton("ПОКАЗАТЬ В ПАПКЕ")
        self.reveal_btn.setProperty("variant", "ghost")
        self.reveal_btn.clicked.connect(self._reveal_selected)
        fhead.addWidget(self.reveal_btn)
        flay.addLayout(fhead)
        self.files_list = QListWidget()
        self.files_list.setToolTip("Двойной клик — показать файл в папке")
        self.files_list.itemDoubleClicked.connect(lambda _: self._reveal_selected())
        flay.addWidget(self.files_list, 1)
        empty = QLabel("Файлов пока нет — отправьте что-нибудь с телефона.")
        empty.setProperty("role", "muted")
        empty.setAlignment(Qt.AlignCenter)
        flay.addWidget(empty)
        self.files_empty_label = empty
        grid.addWidget(files_card, 3, 1)

        # -- футер -----------------------------------------------------------
        footer = QLabel(
            f"Перетащите файлы в это окно — они появятся в списке на телефоне.  "
            f"Папка: {self.directory}"
        )
        footer.setProperty("role", "muted")
        footer.setWordWrap(True)
        footer.setStyleSheet("font-size:11px;")
        root.addWidget(footer)

        self.statusBar().showMessage("Запуск сервера…")

    def _set_url_text(self, url: str):
        self.url_label.setText(
            f'<a href="{url}" style="color:{ACCENT};text-decoration:none;">{url}</a>'
        )

    def _set_chip(self, text: str, state: str):
        self.status_chip.setText(text)
        self.status_chip.setProperty("chipState", state)
        self.status_chip.style().unpolish(self.status_chip)
        self.status_chip.style().polish(self.status_chip)

    # ------------------------------------------------------------- сигналы
    def _connect_signals(self):
        s = self.signals
        s.started.connect(self._on_started)
        s.stopped.connect(self._on_stopped)
        s.error.connect(self._on_error)
        s.clients_changed.connect(self._on_clients)
        s.file_received.connect(self._on_file_received)
        s.text_received.connect(self._on_text_received)
        s.progress.connect(self._on_progress)
        s.log.connect(self._on_log)
        s.files_changed.connect(lambda: self.statusBar().showMessage("Общая папка обновлена", 3000))

    def _on_started(self, url: str):
        self.url = url
        self._server_online = True
        self._set_url_text(url)
        self.qr_label.setPixmap(make_qr_pixmap(url, 232))
        self._set_chip("СЕРВЕР ОНЛАЙН", "online")
        self.statusBar().showMessage(f"Сервер запущен: {url}", 6000)
        self._clip_timer.start()
        if self.tray:
            self.tray.setToolTip(f"DropWave — {url}")

    def _on_stopped(self):
        self._server_online = False
        self._clip_timer.stop()
        if not self._force_quit:
            self._set_chip("ОСТАНОВЛЕН", "")

    def _on_error(self, message: str):
        self._server_online = False
        self._set_chip("ОШИБКА СЕРВЕРА", "error")
        self.statusBar().showMessage(message)
        QMessageBox.critical(self, "DropWave — ошибка сервера", message)

    def _on_clients(self, clients: list):
        had_clients = bool(self._clients)
        self._clients = clients
        self.devices_list.clear()
        dot = self._green_dot_icon()
        for c in clients:
            item = QListWidgetItem(dot, f"{c['name']}    ·    {c['address']}")
            item.setToolTip(f"Подключено с {time.strftime('%H:%M:%S', time.localtime(c['since']))}")
            self.devices_list.addItem(item)
        self.devices_count.setText(f"{len(clients)} ОНЛАЙН")
        self.devices_count.setProperty("chipState", "online" if clients else "")
        self.devices_count.style().unpolish(self.devices_count)
        self.devices_count.style().polish(self.devices_count)
        # Первое подключившееся устройство сразу получает текущий буфер ПК.
        if clients and not had_clients and self._server_online:
            text = QApplication.clipboard().text() or ""
            if text.strip() and len(text) <= MAX_TEXT_LEN and text != self._last_synced_text:
                self._last_synced_text = text
                self.server.send_text_to_clients(text)

    @staticmethod
    def _green_dot_icon() -> QIcon:
        pm = QPixmap(12, 12)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(OK))
        p.drawEllipse(1, 1, 10, 10)
        p.end()
        return QIcon(pm)

    def _on_file_received(self, info: dict):
        stamp = time.strftime("%H:%M:%S")
        text = f"{info['name']}    ·    {format_size(info['size'])}    ·    {stamp}"
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, info.get("path", ""))
        item.setToolTip(f"{info.get('path', '')}\nИсточник: {info.get('client', '?')}")
        self.files_list.insertItem(0, item)
        while self.files_list.count() > MAX_LOG_ITEMS:
            self.files_list.takeItem(self.files_list.count() - 1)
        self.files_empty_label.hide()
        self.statusBar().showMessage(f"Получен файл: {info['name']}", 5000)
        if self.tray and not self.isVisible():
            self.tray.showMessage(
                "DropWave — файл получен",
                f"{info['name']} ({format_size(info['size'])})",
                QSystemTrayIcon.Information, 3000,
            )

    def _on_text_received(self, text: str, source: str):
        self._preview_text(text, f"{source} → ПК")
        # Эхо-защита: запоминаем текст ДО установки в буфер, чтобы монитор
        # не распознал его как «новое копирование» и не отправил обратно.
        self._last_synced_text = text
        if self.auto_paste.isChecked():
            self._last_clip_text = text
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage(f"Текст от «{source}» вставлен в буфер обмена", 4000)

    def _on_progress(self, p: dict):
        tid = p.get("id", "")
        state = p.get("state", "active")
        if state == "active":
            self._transfers[tid] = p
        else:
            self._transfers.pop(tid, None)
        self._render_transfer(p)
        if state != "active" and not self._transfers:
            QTimer.singleShot(3000, self._reset_transfer)

    def _render_transfer(self, p: dict):
        name = p.get("name", "")
        shown = name if len(name) <= 58 else name[:55] + "…"
        direction = "⬇ ПРИЁМ" if p.get("direction") == "upload" else "⬆ ОТДАЧА"
        state = p.get("state", "active")
        self.transfer_name.setToolTip(name)
        if state == "active":
            self.transfer_name.setText(f"{direction}: {shown}")
            total = p.get("total") or 0
            stats = format_size(p.get("loaded", 0))
            if total:
                stats += f" / {format_size(total)}"
            stats += f"  ·  {format_speed(p.get('speed', 0))}"
            if p.get("eta") is not None:
                stats += f"  ·  ост. {format_eta(p['eta'])}"
            self.transfer_stats.setText(stats)
            self.progress.setValue(int(p.get("percent", 0)))
        elif state == "done":
            self.transfer_name.setText(f"✓ ГОТОВО: {shown}")
            self.transfer_stats.setText(format_size(p.get("loaded") or p.get("total") or 0))
            self.progress.setValue(100)
        else:
            self.transfer_name.setText(f"✕ ПРЕРВАНО: {shown}")
            self.transfer_stats.setText("")
            self.progress.setValue(0)

    def _reset_transfer(self):
        if self._transfers:
            self._render_transfer(next(reversed(self._transfers.values())))
            return
        self.transfer_name.setText("Ожидание передач…")
        self.transfer_stats.setText("")
        self.progress.setValue(0)

    def _on_log(self, line: str):
        try:
            print(f"[server] {line}")
        except UnicodeEncodeError:
            print(f"[server] {line.encode('ascii', 'replace').decode()}")
        self.statusBar().showMessage(line, 4000)

    # --------------------------------------------------------- буфер обмена
    def _preview_text(self, text: str, direction: str):
        preview = " ".join(text.split())
        if len(preview) > 120:
            preview = preview[:120] + "…"
        self.clip_preview.setText(f"[{direction}]  {preview}")

    def _poll_clipboard(self):
        """Монитор буфера: новое копирование на ПК → отправка на устройства."""
        if not self._server_online:
            return
        try:
            text = QApplication.clipboard().text() or ""
        except Exception:
            return
        if text == self._last_clip_text:
            return
        self._last_clip_text = text
        if not text.strip() or len(text) > MAX_TEXT_LEN:
            return
        if text == self._last_synced_text:
            return  # эхо нашей же передачи — не отправляем повторно
        self._last_synced_text = text
        self.server.send_text_to_clients(text)
        self._preview_text(text, "ПК → устройства")
        if self._clients:
            self.statusBar().showMessage("Буфер обмена отправлен на устройства", 3000)

    def _send_clipboard_now(self):
        text = QApplication.clipboard().text() or ""
        if not text.strip():
            self.statusBar().showMessage("Буфер обмена пуст", 3000)
            return
        self._last_clip_text = text
        self._last_synced_text = text
        self.server.send_text_to_clients(text)
        self._preview_text(text, "ПК → устройства")
        self.statusBar().showMessage("Текущий буфер отправлен на устройства", 3000)

    def _copy_url(self):
        # Синхронизируем трекеры, чтобы копирование ссылки не улетело на телефон
        # как «новый текст буфера». Основной канал — QClipboard (тот же, что
        # читает монитор), pyperclip — запасной вариант.
        self._last_clip_text = self.url
        self._last_synced_text = self.url
        try:
            QApplication.clipboard().setText(self.url)
        except Exception:
            try:
                pyperclip.copy(self.url)
            except Exception:
                pass
        self.statusBar().showMessage("Ссылка скопирована в буфер обмена", 3000)

    # ------------------------------------------------------------- действия
    def _open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.directory)))

    def _reveal_selected(self):
        item = self.files_list.currentItem()
        if item is None and self.files_list.count():
            item = self.files_list.item(0)
        path = item.data(Qt.UserRole) if item else ""
        if path and Path(path).exists():
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
        else:
            self._open_folder()

    # ------------------------------------------------------------- drag&drop
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(
            u.isLocalFile() for u in event.mimeData().urls()
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        files = [p for p in paths if p.is_file()]
        if not files:
            self.statusBar().showMessage("Перетащите файлы (папки не поддерживаются)", 4000)
            return
        if self._import_worker is not None and self._import_worker.isRunning():
            self.statusBar().showMessage("Копирование уже выполняется, подождите…", 3000)
            return
        self.statusBar().showMessage(f"Копирование файлов: {len(files)}…")
        self._import_worker = FileImportWorker(files, self.directory, self)
        self._import_worker.imported.connect(
            lambda info: self.statusBar().showMessage(f"Добавлен: {info['name']}", 3000))
        self._import_worker.failed.connect(
            lambda msg: self.statusBar().showMessage(f"Ошибка: {msg}", 5000))
        self._import_worker.finished_count.connect(self._on_import_done)
        self._import_worker.start()

    def _on_import_done(self, count: int):
        self.server.notify_files_changed()
        self.statusBar().showMessage(
            f"Добавлено файлов: {count} — доступны для скачивания на телефоне", 6000)
        if self.tray and not self.isVisible() and count:
            self.tray.showMessage(
                "DropWave", f"Файлов добавлено: {count}. Список на телефоне обновлён.",
                QSystemTrayIcon.Information, 3000)

    # ------------------------------------------------------------------ трей
    def _setup_tray(self, use_tray: bool):
        self.tray = None
        if use_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_app_icon(), self)
            self.tray.setToolTip(f"DropWave — {self.url}")
            menu = QMenu()
            show_action = menu.addAction("Показать окно")
            show_action.triggered.connect(self._restore_window)
            menu.addSeparator()
            quit_action = menu.addAction("Выход")
            quit_action.triggered.connect(self._quit_app)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._restore_window()

    def _restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self.tray is not None and self.tray.isVisible() and not self._force_quit:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self.tray.showMessage(
                    "DropWave свёрнут в трей",
                    "Сервер продолжает работать. Двойной клик по иконке — открыть окно, "
                    "правый клик — выход.",
                    QSystemTrayIcon.Information, 4000,
                )
                self._tray_hint_shown = True
        else:
            self._shutdown()
            event.accept()

    def _quit_app(self):
        self._force_quit = True
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._clip_timer.stop()
        self.server.stop()
        if not self.server.wait(7000):
            print("[warn] сервер не остановился за 7 секунд")
        if self.tray is not None:
            self.tray.hide()