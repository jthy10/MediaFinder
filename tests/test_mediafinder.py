import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from mediafinder import db
from mediafinder.app import create_app
from mediafinder.config import ConfigStore, Settings, make_source
from mediafinder.sources import discovery, folder, jellyfin, smb


def touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


def make_tree(tmp_path):
    touch(tmp_path / "Movies/The Matrix (1999)/The Matrix (1999).mkv")
    touch(tmp_path / "Movies/Alien (1979)/Alien (1979).mkv")
    touch(tmp_path / "Movies/Aliens (1986)/Aliens (1986).mkv")
    touch(tmp_path / "TV/Breaking Bad/Season 01/Breaking.Bad.S01E01.mkv")
    touch(tmp_path / "TV/Breaking Bad/Season 01/Breaking.Bad.S01E02.mkv")
    touch(tmp_path / "TV/Breaking Bad/Season 01/notes.txt")
    touch(tmp_path / "Movies/@eaDir/junk (2000).mkv")


def test_folder_scan_root_mode(tmp_path):
    make_tree(tmp_path)
    src = make_source("local", path=str(tmp_path))
    got = {(i.library, i.type, i.title, i.year) for i in folder.scan_source(src)}
    assert ("Movies", "movie", "The Matrix", 1999) in got
    assert ("TV", "series", "Breaking Bad", None) in got
    assert len(got) == 4  # episodes collapse; @eaDir ignored


def test_folder_scan_single_library_mode(tmp_path):
    make_tree(tmp_path)
    src = make_source("local", path=str(tmp_path / "Movies"), mode="library", label="Films")
    items = folder.scan_source(src)
    assert {i.library for i in items} == {"Movies"}
    assert len(items) == 3


def test_smb_walk_and_scan():
    files = {
        "/": ["Movies", "TV", "@eaDir"], "/@eaDir": ["x.mkv"],
        "/Movies": ["Alien (1979)"], "/Movies/Alien (1979)": ["Alien (1979).mkv", "poster.jpg"],
        "/TV": ["Lost"], "/TV/Lost": ["Season 1"], "/TV/Lost/Season 1": ["Lost.S01E01.mkv"],
    }
    def entry(parent, name):
        full = (parent.rstrip("/") + "/" + name)
        return SimpleNamespace(filename=name, isDirectory=full in files)
    class FakeConn:
        def listPath(self, share, path):
            return [entry(path, n) for n in files[path]]
        def close(self): pass
    paths = sorted(smb.walk(FakeConn(), "media", "/"))
    assert paths == ["Movies/Alien (1979)/Alien (1979).mkv", "Movies/Alien (1979)/poster.jpg",
                     "TV/Lost/Season 1/Lost.S01E01.mkv"]
    items = folder.items_from_files("nas", "media", "smb://nas/media", paths)
    assert {(i.library, i.title, i.type) for i in items} == {
        ("Movies", "Alien", "movie"), ("TV", "Lost", "series")}
    assert [i.path for i in items if i.type == "series"] == ["smb://nas/media/TV/Lost"]


def test_jellyfin_fetch_and_auth():
    def handler(req: httpx.Request):
        if req.url.path == "/Users/AuthenticateByName":
            ok = json.loads(req.content)["Pw"] == "pw"
            return httpx.Response(200 if ok else 401, json={"AccessToken": "tok", "User": {"Id": "u1"}})
        assert req.headers["X-Emby-Token"] == "key"
        if req.url.path == "/Library/VirtualFolders":
            return httpx.Response(200, json=[{"Name": "Films", "ItemId": "abc"}])
        return httpx.Response(200, json={"TotalRecordCount": 1, "Items": [
            {"Name": "Alien", "Type": "Movie", "ProductionYear": 1979, "Genres": ["Horror"]}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = jellyfin.fetch_items("http://jf", "key", client=client)
    assert items[0].title == "Alien" and items[0].library == "Films" and items[0].year == 1979
    assert jellyfin.authenticate("http://jf", "me", "pw", client) == {"token": "tok", "user_id": "u1"}
    try:
        jellyfin.authenticate("http://jf", "me", "bad", client)
        assert False
    except PermissionError:
        pass


def test_jellyfin_udp_discovery():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    def serve():
        data, addr = srv.recvfrom(1024)
        assert data == b"who is JellyfinServer?"
        srv.sendto(json.dumps({"Address": "http://10.0.0.5:8096", "Id": "x", "Name": "Home"}).encode(), addr)
    threading.Thread(target=serve, daemon=True).start()
    found = discovery.discover_jellyfin_udp(timeout=1.0, port=port, targets=["127.0.0.1"])
    assert found[0]["name"] == "Home" and found[0]["url"] == "http://10.0.0.5:8096"
    assert discovery.parse_jellyfin_reply(b"garbage") is None


def test_scan_port_finds_open_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0)); srv.listen()
    port = srv.getsockname()[1]
    assert discovery.scan_port("127.0.0.1/32", port) == ["127.0.0.1"]
    try:
        discovery.scan_port("10.0.0.0/8", 445)
        assert False
    except ValueError:
        pass


def test_config_store_persists_and_hides_secrets(tmp_path):
    s = Settings(db_path=str(tmp_path / "d" / "t.db"))
    store = ConfigStore(s)
    store.mutate(lambda d: d["sources"].append(
        make_source("smb", host="nas", share="media", username="u", password="secret")))
    assert ConfigStore(s).snapshot()["sources"][0]["password"] == "secret"
    pub = json.dumps(store.public())
    assert "secret" not in pub and '"has_password": true' in pub


def test_api_end_to_end(tmp_path):
    make_tree(tmp_path)
    s = Settings(db_path=str(tmp_path.parent / (tmp_path.name + "_db") / "t.db"), sync_on_start=False)
    with TestClient(create_app(s)) as c:
        assert c.post("/api/sources", json={"type": "local", "path": "/nope"}).status_code == 400
        r = c.post("/api/sources", json={"type": "local", "path": str(tmp_path), "label": "NAS"})
        assert r.status_code == 200
        assert c.get("/api/config").json()["sources"][0]["label"] == "NAS"
        assert c.get("/api/fs/browse", params={"path": str(tmp_path)}).json()["dirs"] == ["Movies", "TV"]
        c.post("/api/sync")
        for _ in range(100):
            if not c.get("/api/sync/status").json()["running"]:
                break
            time.sleep(0.1)
        assert c.get("/api/search", params={"q": "alie"}).json()["total"] == 2  # prefix match
        assert "Alien" not in c.get("/api/export", params={"format": "csv", "q": "matrix"}).text
        assert c.get("/api/export", params={"format": "txt", "library": "TV"}).text == "Breaking Bad\n"
        assert c.get("/api/stats").json()["total"] == 4
        # removing the source and syncing again empties the index
        sid = c.get("/api/config").json()["sources"][0]["id"]
        c.delete(f"/api/sources/{sid}")
        c.post("/api/sync")
        for _ in range(100):
            if not c.get("/api/sync/status").json()["running"]:
                break
            time.sleep(0.1)
        assert c.get("/api/stats").json()["total"] == 0


def test_replace_source_keeps_other_sources():
    conn = db.connect(":memory:")
    db.replace_source(conn, "a", [db.Item("a", "L", "movie", "X")])
    db.replace_source(conn, "b", [db.Item("b", "L", "movie", "Y")])
    db.replace_source(conn, "a", [])
    assert db.stats(conn)["total"] == 1
