"""Определение сетевого адреса ПК в LAN и выбор свободного порта.

Основной метод получения IP — UDP-сокет к внешнему DNS (8.8.8.8/1.1.1.1).
``connect()`` для UDP не отправляет ни одного пакета: ядро лишь выбирает
маршрут и локальный интерфейс, после чего ``getsockname()`` возвращает IP
адаптера, через который машина выходит в сеть. Виртуальные адаптеры
(Docker, WSL, VMware, VirtualBox, VPN) как правило не имеют маршрута по
умолчанию, поэтому в результат не попадают.

Дополнительно на Windows используется IP Helper API (GetAdaptersInfo),
чтобы отсеять виртуальные адаптеры по имени/описанию и отдать приоритет
адаптерам с шлюзом по умолчанию. На других ОС — эвристики по префиксам.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from dataclasses import dataclass, field

# DNS-цели для UDP-трюка. connect() не генерирует трафик.
_PROBE_TARGETS = (("8.8.8.8", 80), ("1.1.1.1", 80), ("208.67.222.222", 443))

# Подстроки в именах/описаниях виртуальных адаптеров (lowercase).
_VIRTUAL_MARKERS = (
    "vmware", "virtualbox", "vbox", "virtual", "vethernet", "hyper-v",
    "wsl", "docker", "loopback", "tap-windows", "tap-", "wan miniport",
    "vpn", "ppp", "tunnel", "zerotier", "tailscale", "npcap", "ndis",
    "bluetooth", "virtualbox host-only",
)

DEFAULT_PORT_RANGE = (8000, 8090)


class NetworkError(RuntimeError):
    """Базовая ошибка сетевого модуля."""


class NoFreePortError(NetworkError):
    """В заданном диапазоне не нашлось свободного порта."""


class NoLanAddressError(NetworkError):
    """Не удалось определить пригодный LAN-адрес."""


@dataclass
class InterfaceInfo:
    """Сведения об IPv4-адресе сетевого адаптера."""

    ip: str
    name: str = ""
    description: str = ""
    has_gateway: bool = False
    if_type: int = 0  # 6 = ethernet, 71 = wifi, 24 = loopback, 23 = ppp, 131 = tunnel
    is_virtual: bool = False

    @property
    def score(self) -> int:
        """Приоритет адреса: чем выше, тем вероятнее реальный LAN."""
        s = 0
        if self.is_virtual:
            s -= 1000
        try:
            addr = ipaddress.IPv4Address(self.ip)
        except ValueError:
            return -2000
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            s -= 2000
        if addr.is_multicast or addr.is_reserved:
            s -= 2000
        if not addr.is_private:
            s -= 500  # публичный IP на хосте — редкий случай, понижаем приоритет
        if self.has_gateway:
            s += 100
        if self.if_type in (6, 71):  # ethernet / wifi
            s += 20
        if self.ip.startswith("192.168."):
            s += 6
        elif self.ip.startswith("10."):
            s += 5
        elif self.ip.startswith("172."):
            s += 2  # 172.16/12 бывает и у Docker/WSL — без шлюза отсеется
        return s


def _looks_virtual_text(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _VIRTUAL_MARKERS)


def _looks_virtual_ip(ip: str) -> bool:
    """Эвристика для ОС без перечисления адаптеров."""
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return True
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return True
    # Docker bridge по умолчанию: 172.17.0.0/16 … 172.31.x (WSL тоже часто тут).
    if addr in ipaddress.IPv4Network("172.17.0.0/16"):
        return True
    return False


# ---------------------------------------------------------------------------
# Windows: перечисление адаптеров через IP Helper API (без внешних зависимостей)
# ---------------------------------------------------------------------------

def _windows_interfaces() -> list[InterfaceInfo]:
    """Список IPv4-адресов адаптеров Windows через GetAdaptersInfo.

    Любой сбой API (редкая редакция Windows, повреждённый стек) приводит к
    пустому списку — вызывающий код перейдёт к запасным методам.
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class IP_ADDR_STRING(ctypes.Structure):
            pass

        IP_ADDR_STRING._fields_ = [
            ("Next", ctypes.POINTER(IP_ADDR_STRING)),
            ("IpAddress", ctypes.c_char * 16),
            ("IpMask", ctypes.c_char * 16),
            ("Context", ctypes.c_ulong),
        ]

        class IP_ADAPTER_INFO(ctypes.Structure):
            pass

        IP_ADAPTER_INFO._fields_ = [
            ("Next", ctypes.POINTER(IP_ADAPTER_INFO)),
            ("ComboIndex", ctypes.c_ulong),
            ("AdapterName", ctypes.c_char * 260),
            ("Description", ctypes.c_char * 132),
            ("AddressLength", ctypes.c_uint),
            ("Address", ctypes.c_byte * 8),
            ("Index", ctypes.c_ulong),
            ("Type", ctypes.c_uint),
            ("DhcpEnabled", ctypes.c_uint),
            ("CurrentIpAddress", ctypes.POINTER(IP_ADDR_STRING)),
            ("IpAddressList", IP_ADDR_STRING),
            ("GatewayList", IP_ADDR_STRING),
            ("DhcpServerList", IP_ADDR_STRING),
            ("HaveWins", wintypes.BOOL),
            ("PrimaryWinsServerList", IP_ADDR_STRING),
            ("SecondaryWinsServerList", IP_ADDR_STRING),
            ("LeaseObtained", ctypes.c_longlong),
            ("LeaseExpires", ctypes.c_longlong),
        ]

        iphlpapi = ctypes.windll.iphlpapi
        size = wintypes.ULONG(0)
        # ERROR_BUFFER_OVERFLOW = 111
        if iphlpapi.GetAdaptersInfo(None, ctypes.byref(size)) != 111 or size.value == 0:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetAdaptersInfo(buf, ctypes.byref(size)) != 0:
            return []

        result: list[InterfaceInfo] = []
        adapter = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_INFO))
        while adapter:
            a = adapter.contents
            try:
                name = a.AdapterName.decode("mbcs", errors="replace")
                desc = a.Description.decode("mbcs", errors="replace")
            except Exception:
                name, desc = "", ""
            virtual = (
                _looks_virtual_text(name)
                or _looks_virtual_text(desc)
                or a.Type in (23, 24, 131)  # ppp / loopback / tunnel
            )
            gateway = a.GatewayList.IpAddress.decode("ascii", errors="ignore").strip("\x00 ")
            has_gw = bool(gateway) and gateway not in ("0.0.0.0", "")
            node = a.IpAddressList
            # IpAddressList — встроенная структура; следующие адреса через Next.
            addrs = [node.IpAddress.decode("ascii", errors="ignore").strip("\x00 ")]
            nxt = node.Next
            guard = 0
            while nxt and guard < 32:
                addrs.append(nxt.contents.IpAddress.decode("ascii", errors="ignore").strip("\x00 "))
                nxt = nxt.contents.Next
                guard += 1
            for ip in addrs:
                if not ip or ip in ("0.0.0.0", "127.0.0.1"):
                    continue
                result.append(
                    InterfaceInfo(
                        ip=ip,
                        name=name,
                        description=desc,
                        has_gateway=has_gw,
                        if_type=int(a.Type),
                        is_virtual=virtual or _looks_virtual_ip(ip),
                    )
                )
            if not a.Next:
                break
            adapter = a.Next
        return result
    except Exception:
        # Не роняем приложение из-за сбоя вспомогательного API.
        return []


def _generic_interfaces() -> list[InterfaceInfo]:
    """Запасное перечисление адресов через getaddrinfo (все ОС)."""
    result: list[InterfaceInfo] = []
    seen: set[str] = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip in seen:
            continue
        seen.add(ip)
        result.append(
            InterfaceInfo(ip=ip, is_virtual=_looks_virtual_ip(ip))
        )
    return result


def _udp_probe_ip() -> str | None:
    """IP интерфейса с маршрутом по умолчанию (UDP connect без трафика)."""
    for host, port in _PROBE_TARGETS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.connect((host, port))  # пакеты не отправляются
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    return None


def list_interfaces() -> list[InterfaceInfo]:
    """Все известные IPv4-адресы adapters, лучшие первыми."""
    ifaces = _windows_interfaces() if sys.platform == "win32" else []
    if not ifaces:
        ifaces = _generic_interfaces()
    probe = _udp_probe_ip()
    if probe:
        # UDP-трюк — самый надёжный признак: помечаем адрес как «шлюзовой».
        for iface in ifaces:
            if iface.ip == probe:
                iface.has_gateway = True
                iface.is_virtual = False
                break
        else:
            ifaces.append(InterfaceInfo(ip=probe, name="default-route", has_gateway=True))
    return sorted(ifaces, key=lambda i: i.score, reverse=True)


def get_local_ip() -> str:
    """Реальный LAN-адрес ПК. Бросает NoLanAddressError, если адрес не найден."""
    ifaces = list_interfaces()
    usable = [i for i in ifaces if i.score > -500]
    if not usable:
        raise NoLanAddressError(
            "Не удалось определить LAN-адрес. Проверьте, что ПК подключён "
            "к Wi-Fi/Ethernet сети (не только к VPN или виртуальному адаптеру)."
        )
    return usable[0].ip


# ---------------------------------------------------------------------------
# Порты
# ---------------------------------------------------------------------------

def is_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """Проверка порта фактической привязкой сокета (без SO_REUSEADDR)."""
    if not (0 < port < 65536):
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_free_port(
    preferred: int | None = None,
    port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
    host: str = "0.0.0.0",
) -> int:
    """Свободный порт: сначала preferred, затем диапазон port_range включительно.

    Бросает NoFreePortError, если все порты заняты или диапазон некорректен.
    """
    start, stop = port_range
    if not (0 < start <= stop < 65536):
        raise NoFreePortError(f"Некорректный диапазон портов: {start}-{stop}")
    candidates: list[int] = []
    if preferred is not None:
        if not (0 < preferred < 65536):
            raise NoFreePortError(f"Некорректный порт: {preferred}")
        candidates.append(preferred)
    candidates.extend(p for p in range(start, stop + 1) if p != preferred)
    for port in candidates:
        if is_port_free(port, host):
            return port
    raise NoFreePortError(
        f"Все порты диапазона {start}-{stop} заняты. Закройте другое приложение "
        "или укажите порт вручную: python main.py --port 8100"
    )


@dataclass
class NetworkInfo:
    """Итоговые параметры для запуска сервера."""

    ip: str
    port: int
    url: str
    interfaces: list[InterfaceInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def resolve_network(preferred_port: int | None = None) -> NetworkInfo:
    """Определить IP и порт, собрать предупреждения для UI."""
    warnings: list[str] = []
    ifaces = list_interfaces()
    try:
        ip = get_local_ip()
    except NoLanAddressError:
        ip = "127.0.0.1"
        warnings.append(
            "LAN-адрес не найден — сервер доступен только на этом ПК (127.0.0.1). "
            "Подключитесь к Wi-Fi/Ethernet и перезапустите DropWave."
        )
    port = find_free_port(preferred=preferred_port)
    if preferred_port is not None and port != preferred_port:
        warnings.append(f"Порт {preferred_port} занят, выбран свободный {port}.")
    if ip != "127.0.0.1" and not any(i.ip == ip and i.has_gateway for i in ifaces):
        warnings.append("Адрес определён без шлюза по умолчанию — телефон может не подключиться.")
    if sys.platform == "win32":
        warnings.append(
            "Если телефон не открывает страницу — разрешите Python входящие "
            "подключения в брандмауэре Windows (частные сети)."
        )
    return NetworkInfo(
        ip=ip,
        port=port,
        url=f"http://{ip}:{port}",
        interfaces=ifaces,
        warnings=warnings,
    )