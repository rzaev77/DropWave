**Структура (запуск из корня: ```python main.py```)**

core/network.py — LAN IP через UDP-сокет к DNS (без трафика), на Windows дополнительно фильтрует виртуальные адаптеры (Docker/WSL/VMware/VPN) через IP Helper API; подбор свободного порта 8000–8090.

core/server.py — FastAPI в отдельном QThread со своим asyncio-loop: POST /upload (стрим с буфером 1 МБ, уникальные имена без перезаписи), GET /download/{name} (потоковая отдача, RFC 5987 Content-Disposition), WebSocket /ws с пингами, прогрессом и синхронизацией текста; автоматический retry на следующий порт при занятом.

core/storage.py — папка <Downloads>/DropWave, санитизация имён, защита от path traversal, проверка прав записи.

web/index.html — мобильный тёмный UI: drag&drop/тап, живые прогресс-бары со скоростью и ETA, отправка текста + «Вставить из буфера», список файлов ПК. Tailwind через CDN подключён, но вся вёрстка продублирована собственным 

CSS — интерфейс полностью работает без интернета.

ui/main_window.py + ui/theme.py — нео-брутализм: чёткий QR, кликабельный URL (клик = копирование), индикатор устройств, лог файлов с «Показать в папке», авто-вставка в буфер Windows с эхо-защитой, трей при закрытии, drag&drop файлов в окно.

tests/smoke_test.py — python tests/smoke_test.py: реальный сервер, загрузка 3 МБ, дубликаты имён, chunked-режим, скачивание, traversal, WS в обе стороны, GUI и рекурсия буфера.# DropWave
