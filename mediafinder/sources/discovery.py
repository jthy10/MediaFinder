"""Find Jellyfin servers and NAS (SMB) hosts on the local network.

* Jellyfin answers a UDP broadcast ("who is JellyfinServer?" on port 7359) - the same
  mechanism its own apps use. This needs the container on the host network
  (``network_mode: host``) so the broadcast actually reaches your LAN.
* As a fallback (and for SMB, which has no simple broadcast) we scan a subnet for open
  ports: 8096 for Jellyfin, 445 for SMB. Works from a normal bridged container too, as
  long as you give it your LAN's CIDR (e.g. 192.168.1.0/24).
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

JELLYFIN_UDP_PORT = 7359
MAX_HOSTS = 1024


def local_ips() -> list[str]:
    ips: set[str] = set()
    try:  # no packets are sent; this just asks the OS which interface routes outward
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "0.")))


def local_subnets() -> list[str]:
    return sorted({str(ipaddress.ip_network(f"{ip}/24", strict=False)) for ip in local_ips()})


def parse_jellyfin_reply(data: bytes) -> dict | None:
    try:
        d = json.loads(data.decode("utf-8", "replace"))
        return {"name": d["Name"], "url": d["Address"].rstrip("/"), "id": d.get("Id"),
                "found_by": "broadcast"}
    except (ValueError, KeyError, AttributeError):
        return None


def discover_jellyfin_udp(timeout: float = 2.0, port: int = JELLYFIN_UDP_PORT,
                          targets: list[str] | None = None) -> list[dict]:
    if targets is None:
        targets = ["255.255.255.255"] + [
            str(ipaddress.ip_network(f"{ip}/24", strict=False).broadcast_address) for ip in local_ips()]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.25)
    for t in dict.fromkeys(targets):
        try:
            sock.sendto(b"who is JellyfinServer?", (t, port))
        except OSError:
            pass
    found: dict[str, dict] = {}
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if (srv := parse_jellyfin_reply(data)):
            found[srv["url"]] = srv
    sock.close()
    return list(found.values())


def scan_port(cidr: str, port: int, timeout: float = 0.5, workers: int = 200) -> list[str]:
    net = ipaddress.ip_network(cidr, strict=False)
    if net.version != 4:
        raise ValueError("Only IPv4 ranges are supported")
    if net.num_addresses > MAX_HOSTS:
        raise ValueError(f"Range too large (max {MAX_HOSTS} addresses, e.g. a /22)")
    hosts = [str(h) for h in (net.hosts() if net.num_addresses > 2 else net)]

    def probe(ip: str) -> str | None:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return ip
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [ip for ip in ex.map(probe, hosts) if ip]


def discover_jellyfin_scan(cidr: str, port: int = 8096) -> list[dict]:
    found = []
    with httpx.Client(timeout=2) as client:
        for ip in scan_port(cidr, port):
            try:
                r = client.get(f"http://{ip}:{port}/System/Info/Public")
                d = r.json()
                if "ServerName" in d:
                    found.append({"name": d["ServerName"], "url": f"http://{ip}:{port}",
                                  "id": d.get("Id"), "version": d.get("Version"),
                                  "found_by": "scan"})
            except (httpx.HTTPError, ValueError):
                continue
    return found


def discover_smb(cidr: str) -> list[dict]:
    ips = scan_port(cidr, 445)

    def name(ip: str) -> str:
        try:
            return socket.gethostbyaddr(ip)[0]
        except OSError:
            return ""

    with ThreadPoolExecutor(max_workers=32) as ex:
        return [{"ip": ip, "name": n} for ip, n in zip(ips, ex.map(name, ips))]
