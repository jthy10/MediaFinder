"""Browse and walk SMB/CIFS shares (Synology, QNAP, TrueNAS, Windows, Samba...) directly.

No mounting needed, so the container doesn't require extra privileges.
"""
from __future__ import annotations

from typing import Iterator

from smb.SMBConnection import SMBConnection

from .folder import skip_dir

DISK_SHARE = 0


def connect(host: str, username: str = "", password: str = "", domain: str = "",
            port: int = 445, timeout: int = 10) -> SMBConnection:
    conn = SMBConnection(username or "guest", password or "", "mediafinder", host,
                         domain=domain or "", use_ntlm_v2=True, is_direct_tcp=True)
    if not conn.connect(host, port, timeout=timeout):
        raise ConnectionError(f"Could not authenticate to {host} (check username/password)")
    return conn


def list_shares(conn: SMBConnection) -> list[str]:
    return sorted(s.name for s in conn.listShares()
                  if s.type == DISK_SHARE and not s.name.endswith("$") and not s.isSpecial)


def _abs(path: str) -> str:
    return "/" + path.strip("/")


def list_dirs(conn: SMBConnection, share: str, path: str = "/") -> list[str]:
    return sorted(e.filename for e in conn.listPath(share, _abs(path))
                  if e.isDirectory and e.filename not in (".", "..") and not skip_dir(e.filename, top=True))


def walk(conn: SMBConnection, share: str, base: str = "/") -> Iterator[str]:
    """Yield file paths (posix, relative to ``base``) under a share folder."""
    stack = [""]
    while stack:
        rel = stack.pop()
        try:
            entries = conn.listPath(share, _abs(f"{base.strip('/')}/{rel}"))
        except Exception:
            if rel == "":
                raise  # can't read the root at all: surface the error
            continue   # unreadable subfolder: skip
        for e in entries:
            if e.filename in (".", ".."):
                continue
            child = f"{rel}/{e.filename}" if rel else e.filename
            if e.isDirectory:
                if not skip_dir(e.filename):
                    stack.append(child)
            else:
                yield child


def scan_source(src: dict) -> list:
    from .folder import items_from_files
    conn = connect(src["host"], src["username"], src["password"], src.get("domain", ""))
    try:
        root_name = src["path"].strip("/").split("/")[-1] or src["share"]
        display = f"smb://{src['host']}/{src['share']}{src['path'].rstrip('/')}"
        return items_from_files(src["label"], root_name, display,
                                walk(conn, src["share"], src["path"]),
                                src.get("mode", "root"), src.get("hint", "auto"))
    finally:
        conn.close()
