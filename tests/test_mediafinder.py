import httpx
from fastapi.testclient import TestClient

from mediafinder import db
from mediafinder.app import create_app
from mediafinder.config import Settings
from mediafinder.sources import folder, jellyfin


def touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


def test_folder_scan(tmp_path):
    touch(tmp_path / "Movies/The Matrix (1999)/The Matrix (1999).mkv")
    touch(tmp_path / "TV/Breaking Bad/Season 01/Breaking.Bad.S01E01.mkv")
    touch(tmp_path / "TV/Breaking Bad/Season 01/Breaking.Bad.S01E02.mkv")
    touch(tmp_path / "TV/Breaking Bad/Season 01/notes.txt")
    items = folder.scan_root(str(tmp_path))
    got = {(i.library, i.type, i.title, i.year) for i in items}
    assert ("Movies", "movie", "The Matrix", 1999) in got
    assert ("TV", "series", "Breaking Bad", None) in got
    assert len(items) == 2  # episodes collapse into one series entry


def test_jellyfin_fetch():
    def handler(req: httpx.Request):
        assert req.headers["X-Emby-Token"] == "key"
        if req.url.path == "/Library/VirtualFolders":
            return httpx.Response(200, json=[{"Name": "Films", "ItemId": "abc"}])
        return httpx.Response(200, json={"TotalRecordCount": 1, "Items": [
            {"Name": "Alien", "Type": "Movie", "ProductionYear": 1979, "Genres": ["Horror"]}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = jellyfin.fetch_items("http://jf", "key", client)
    assert items[0].title == "Alien" and items[0].library == "Films" and items[0].year == 1979


def test_search_and_export(tmp_path):
    touch(tmp_path / "Movies/Alien (1979)/Alien (1979).mkv")
    touch(tmp_path / "Movies/Aliens (1986)/Aliens (1986).mkv")
    touch(tmp_path / "Movies/Heat (1995)/Heat (1995).mkv")
    s = Settings(media_paths=[str(tmp_path)], db_path=str(tmp_path / "d" / "t.db"), sync_on_start=False)
    with TestClient(create_app(s)) as c:
        assert c.post("/api/sync").status_code == 202
        import time
        for _ in range(100):
            if not c.get("/api/sync/status").json()["running"]:
                break
            time.sleep(0.1)
        r = c.get("/api/search", params={"q": "alie"}).json()  # prefix match
        assert r["total"] == 2
        csv_body = c.get("/api/export", params={"format": "csv", "q": "heat"}).text
        assert "Heat" in csv_body and "Alien" not in csv_body
        assert c.get("/api/export", params={"format": "txt"}).text.splitlines()[0] == "Alien (1979)"
        assert c.get("/api/stats").json()["total"] == 3


def test_replace_source_keeps_other_sources():
    conn = db.connect(":memory:")
    db.replace_source(conn, "a", [db.Item("a", "L", "movie", "X")])
    db.replace_source(conn, "b", [db.Item("b", "L", "movie", "Y")])
    db.replace_source(conn, "a", [])
    assert db.stats(conn)["total"] == 1
