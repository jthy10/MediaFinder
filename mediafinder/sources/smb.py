"""Browse and walk SMB shares (Synology, QNAP, TrueNAS, Unraid, Windows, Samba...).

Uses `smbprotocol`, which speaks SMB 2 and 3 including signing and encryption, so it works
with NAS boxes that have SMB1/2 disabled or require encryption. Nothing is mounted, so the
container needs no extra privileges.

Listing the shares a server offers needs a different (RPC) call that smbprotocol lacks, so
that one step uses `pysmb` on a best-effort basis; if it fails you can type the share name.
"""
from __future__ import annotations

from typing import Callable, Iterator

import logging

import smbclient

from .folder import items_from_files, skip_dir

TIMEOUT = 15
logging.getLogger("smbprotocol").setLevel(logging.ERROR)   # silence routine disconnect chatter


def _username(username: str, domain: str) -> str:
    username = username or "guest"
    return f"{domain}\\{username}" if domain and "\\" not in username and "@" not in username else username


class Session:
    """One authenticated connection to a NAS, closed when the block ends."""

    def __init__(self, host: str, username: str = "", password: str = "", domain: str = ""):
        self.host = host
        self._cache: dict = {}
        user, pw = _username(username, domain), password or ""
        # Sign by default: Windows/domain servers and hardened NAS boxes require it and
        # answer STATUS_ACCESS_DENIED otherwise. Guest logins and old servers can't sign,
        # so fall back to unsigned if a signed session can't be established.
        try:
            smbclient.register_session(host, username=user, password=pw, connection_cache=self._cache,
                                       connection_timeout=TIMEOUT, require_signing=True)
        except Exception:
            self._cache.clear()
            smbclient.register_session(host, username=user, password=pw, connection_cache=self._cache,
                                       connection_timeout=TIMEOUT, require_signing=False)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        smbclient.reset_connection_cache(connection_cache=self._cache)

    def unc(self, share: str, path: str = "") -> str:
        rest = "\\".join(p for p in path.replace("\\", "/").split("/") if p)
        return f"\\\\{self.host}\\{share}" + (f"\\{rest}" if rest else "")

    def list_dirs(self, share: str, path: str = "/") -> list[str]:
        return sorted(e.name for e in smbclient.scandir(self.unc(share, path), connection_cache=self._cache)
                      if e.is_dir() and not skip_dir(e.name, top=True))

    def walk(self, share: str, base: str = "/") -> Iterator[str]:
        """Yield file paths (posix, relative to ``base``) under a share folder."""
        stack = [""]
        while stack:
            rel = stack.pop()
            try:
                entries = list(smbclient.scandir(self.unc(share, f"{base.strip('/')}/{rel}"),
                                                 connection_cache=self._cache))
            except Exception:
                if rel == "":
                    raise  # can't read the root at all: surface the error
                continue   # unreadable sub-folder: skip it
            for e in entries:
                child = f"{rel}/{e.name}" if rel else e.name
                if e.is_dir():
                    if not skip_dir(e.name):
                        stack.append(child)
                else:
                    yield child


def list_shares(host: str, username: str = "", password: str = "", domain: str = "") -> list[str]:
    """Best-effort share enumeration (uses pysmb's RPC support)."""
    from smb.SMBConnection import SMBConnection
    conn = SMBConnection(username or "guest", password or "", "mediafinder", host,
                         domain=domain or "", use_ntlm_v2=True, is_direct_tcp=True)
    if not conn.connect(host, 445, timeout=TIMEOUT):
        raise ConnectionError("authentication failed")
    try:
        return sorted(s.name for s in conn.listShares()
                      if s.type == 0 and not s.name.endswith("$") and not s.isSpecial)
    finally:
        conn.close()


def scan_source(src: dict, progress: Callable[[int], None] | None = None) -> list:
    root_name = src["path"].strip("/").split("/")[-1] or src["share"]
    display = f"smb://{src['host']}/{src['share']}{src['path'].rstrip('/')}"
    with Session(src["host"], src["username"], src["password"], src.get("domain", "")) as s:
        return items_from_files(src["label"], root_name, display, s.walk(src["share"], src["path"]),
                                src.get("mode", "root"), src.get("hint", "auto"), progress)
