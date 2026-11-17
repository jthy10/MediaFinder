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


class FakeSmbclient:
    """Stands in for smbclient (smbprotocol) with an in-memory share."""
    def __init__(self, tree):
        self.tree, self.registered = tree, None
    def register_session(self, host, **kw):
        self.registered = (host, kw)
    def reset_connection_cache(self, **kw):
        pass
    def scandir(self, unc, **kw):
        parts = [p for p in unc.split("\\") if p]
        path = "/" + "/".join(parts[2:])
        if path not in self.tree:
            raise OSError("no such dir")
        return [SimpleNamespace(name=n, is_dir=(lambda full=(path.rstrip("/") + "/" + n): full in self.tree))
                for n in self.tree[path]]


def test_smb_walk_and_scan(monkeypatch):
    tree = {
        "/": ["Movies", "TV", "@eaDir"], "/@eaDir": ["x.mkv"],
        "/Movies": ["Alien (1979)"], "/Movies/Alien (1979)": ["Alien (1979).mkv", "poster.jpg"],
        "/TV": ["Lost"], "/TV/Lost": ["Season 1"], "/TV/Lost/Season 1": ["Lost.S01E01.mkv"],
    }
    fake = FakeSmbclient(tree)
    monkeypatch.setattr(smb, "smbclient", fake)
    with smb.Session("nas", "me", "pw", "HOME") as s:
        assert fake.registered[0] == "nas" and fake.registered[1]["username"] == "HOME\me"
        assert s.list_dirs("media", "/") == ["Movies", "TV"]          # @eaDir hidden
        paths = sorted(s.walk("media", "/"))
    assert paths == ["Movies/Alien (1979)/Alien (1979).mkv", "Movies/Alien (1979)/poster.jpg",
                     "TV/Lost/Season 1/Lost.S01E01.mkv"]
    items = folder.items_from_files("nas", "media", "smb://nas/media", paths)
    assert {(i.library, i.title, i.type) for i in items} == {
        ("Movies", "Alien", "movie"), ("TV", "Lost", "series")}
    assert [i.path for i in items if i.type == "series"] == ["smb://nas/media/TV/Lost"]
    src = make_source("smb", host="nas", share="media", username="me", password="pw")
    assert len(smb.scan_source(src)) == 2


def test_jellyfin_fetch_and_auth():
    def handler(req: httpx.Request):
        if req.url.path == "/Users/AuthenticateByName":
            ok = json.loads(req.content)["Pw"] == "pw"
            return httpx.Response(200 if ok else 401, json={"AccessToken": "tok", "User": {"Id": "u1"}})
        assert 'Token="key"' in req.headers["Authorization"]
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
    assert "secret" not in open(store.path).read()        # encrypted on disk
    assert open(store.path).read().count("enc:") == 1


def test_secret_key_from_env_and_wrong_key(tmp_path):
    s = Settings(db_path=str(tmp_path / "d" / "t.db"), secret_key="k1")
    ConfigStore(s).mutate(lambda d: d["sources"].append(make_source("smb", host="h", share="s", password="pw")))
    assert ConfigStore(s).snapshot()["sources"][0]["password"] == "pw"
    import pytest
    with pytest.raises(ValueError):
        ConfigStore(Settings(db_path=s.db_path, secret_key="other"))


def _client(tmp_path, **kw):
    s = Settings(db_path=str(tmp_path / "d" / "t.db"), sync_on_start=False, browse_roots=[str(tmp_path / "media")], **kw)
    (tmp_path / "media" / "Movies").mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(s))


def test_auth_flow(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/api/stats").status_code == 200          # open until a password is set
        assert c.get("/api/auth/status").json()["required"] is False
        assert c.post("/api/auth/password", json={"new": "short"}).status_code == 400
        assert c.post("/api/auth/password", json={"new": "correct horse"}).status_code == 200
        c.cookies.clear()
        assert c.get("/api/stats").status_code == 401
        assert c.get("/api/health").status_code == 200          # public
        assert c.get("/api/auth/status").json() == {"required": True, "authenticated": False, "locked_by_env": False}
        assert c.post("/api/auth/login", json={"password": "nope"}).status_code == 401
        assert c.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
        assert c.get("/api/stats").status_code == 200
        assert "correct horse" not in open(tmp_path / "d" / "config.json").read()
        assert c.post("/api/auth/password", json={"current": "bad", "new": ""}).status_code == 401
        assert c.post("/api/auth/password", json={"current": "correct horse", "new": ""}).status_code == 200
        c.cookies.clear()
        assert c.get("/api/stats").status_code == 200


def test_env_password_and_throttle(tmp_path):
    with _client(tmp_path, admin_password="envpass123") as c:
        assert c.get("/api/stats").status_code == 401
        assert c.post("/api/auth/password", json={"new": "x" * 10}).status_code in (400, 401)
        for _ in range(5):
            assert c.post("/api/auth/login", json={"password": "bad"}).status_code == 401
        assert c.post("/api/auth/login", json={"password": "envpass123"}).status_code == 429  # throttled
    with _client(tmp_path, admin_password="envpass123") as c2:   # fresh throttle state
        assert c2.post("/api/auth/login", json={"password": "envpass123"}).status_code == 200


def test_browse_roots_and_cidr_scoping(tmp_path):
    with _client(tmp_path) as c:
        root = str(tmp_path / "media").replace("\\", "/")
        assert c.get("/api/fs/browse").json()["dirs"][0]["name"] == "Movies"
        assert c.get("/api/fs/browse", params={"path": str(tmp_path)}).status_code == 403
        assert c.get("/api/fs/browse", params={"path": root + "/../"}).status_code == 403   # traversal
        assert c.post("/api/sources", json={"type": "local", "path": str(tmp_path)}).status_code == 403
        assert c.post("/api/sources", json={"type": "local", "path": root + "/Movies"}).status_code == 200
        assert c.get("/api/discover/smb", params={"cidr": "8.8.8.0/24"}).status_code == 400   # public range
        assert c.get("/api/discover/smb", params={"cidr": "garbage"}).status_code == 400


def test_api_end_to_end(tmp_path):
    make_tree(tmp_path)
    s = Settings(db_path=str(tmp_path.parent / (tmp_path.name + "_db") / "t.db"), sync_on_start=False, browse_roots=["*"])
    with TestClient(create_app(s)) as c:
        assert c.post("/api/sources", json={"type": "local", "path": "/nope"}).status_code == 400
        r = c.post("/api/sources", json={"type": "local", "path": str(tmp_path), "label": "NAS"})
        assert r.status_code == 200
        assert c.get("/api/config").json()["sources"][0]["label"] == "NAS"
        assert c.get("/api/fs/browse", params={"path": str(tmp_path)}).json()["dirs"][0]["name"] == "Movies"
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


def test_legacy_plaintext_config_is_migrated(tmp_path):
    s = Settings(db_path=str(tmp_path / "d" / "t.db"))
    cfg = tmp_path / "d" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"sources": [dict(make_source("smb", host="h", share="s", password="oldpw"))]}))
    assert ConfigStore(s).snapshot()["sources"][0]["password"] == "oldpw"
    assert "oldpw" not in cfg.read_text() and "enc:" in cfg.read_text()


def test_incremental_replace_and_fts_stay_in_sync():
    conn = db.connect(":memory:")
    mk = lambda title, path, year=2000: db.Item("s", "L", "movie", title, year, path)
    r = db.replace_source(conn, "s", [mk("Alien", "/a"), mk("Heat", "/h"), mk("Ronin", "/r")])
    assert r == {"total": 3, "added": 3, "updated": 0, "removed": 0}
    # unchanged -> no writes; one retitled, one removed, one new
    r = db.replace_source(conn, "s", [mk("Alien", "/a"), mk("Heat (Remaster)", "/h"), mk("Se7en", "/s")])
    assert r == {"total": 3, "added": 1, "updated": 1, "removed": 1}
    names = lambda q: [i["title"] for i in db.search(conn, q)[1]]
    assert names("remaster") == ["Heat (Remaster)"] and names("ronin") == [] and names("se7") == ["Se7en"]
    assert names("heat") == ["Heat (Remaster)"]
    assert db.replace_source(conn, "s", [mk("Alien", "/a")] * 2)["total"] == 2   # duplicate keys kept


def test_migration_from_v1_database(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(db.BASE_SCHEMA)
    old.execute("INSERT INTO items (source,library,type,title,year,path) VALUES ('s','L','movie','Alien',1979,'/a')")
    old.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    old.commit(); old.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute("SELECT key FROM items").fetchone()[0] == "L/a"
    assert db.search(conn, "alie")[0] == 1
    assert db.replace_source(conn, "s", [db.Item("s", "L", "movie", "Alien", 1979, "/a")])["added"] == 0


def test_sync_status_has_per_source_progress_and_history(tmp_path):
    make_tree(tmp_path)
    s = Settings(db_path=str(tmp_path.parent / (tmp_path.name + "_db") / "t.db"), sync_on_start=False, browse_roots=["*"])
    with TestClient(create_app(s)) as c:
        c.post("/api/sources", json={"type": "local", "path": str(tmp_path), "label": "NAS"})
        before = c.get("/api/sync/status").json()
        assert before["sources"]["NAS"]["state"] == "idle"
        c.post("/api/sync")
        for _ in range(100):
            st = c.get("/api/sync/status").json()
            if not st["running"]:
                break
            time.sleep(0.1)
        nas = st["sources"]["NAS"]
        assert nas["state"] == "done" and nas["total"] == 4 and nas["added"] == 4 and nas["finished"]
        c.post("/api/sync")
        time.sleep(0.5)
        for _ in range(100):
            if not c.get("/api/sync/status").json()["running"]:
                break
            time.sleep(0.1)
        again = c.get("/api/sync/status").json()["sources"]["NAS"]
        assert again["added"] == 0 and again["total"] == 4     # incremental: nothing re-written


def test_smb_signing_default_and_fallback(monkeypatch):
    calls = []
    class Fake(FakeSmbclient):
        def register_session(self, host, **kw):
            calls.append(kw["require_signing"])
            if kw["require_signing"] and host == "old-nas":
                raise OSError("server does not support signing")
    monkeypatch.setattr(smb, "smbclient", Fake({"/": []}))
    smb.Session("modern-nas", "me", "pw").close()
    assert calls == [True]                      # signed by default
    calls.clear()
    smb.Session("old-nas", "me", "pw").close()
    assert calls == [True, False]               # falls back for servers that can't sign


def test_id_tags_in_names_are_parsed_and_stripped():
    files = ["Movies/The Matrix (1999) [tmdbid-603]/The Matrix (1999).mkv",
             "TV/Breaking Bad {tvdb-81189}/Season 01/Breaking.Bad.S01E01.mkv",
             "Movies/Heat.1995.tt0113277.1080p.mkv",
             "Movies/Inception (2010) (imdbid=tt1375666)/Inception.mkv"]
    by_title = {i.title: i for i in folder.items_from_files("s", "r", "/r", files)}
    assert by_title["The Matrix"].tmdb_id == "603"
    assert by_title["Breaking Bad"].tvdb_id == "81189"
    assert by_title["Heat"].imdb_id == "tt0113277" and by_title["Inception"].imdb_id == "tt1375666"


def _it(title, year=None, type="movie", **ids):
    return {"title": title, "year": year, "type": type, "tmdb_id": None, "imdb_id": None, "tvdb_id": None, **ids}


def test_compare_matches_by_id_then_title():
    from mediafinder.compare import compare, norm_title
    assert norm_title("The Matrix: Reloaded!") == norm_title("matrix  reloaded") == norm_title("Matrix Reloaded")
    nas = [_it("Dune", 1984), _it("Dune", 2021), _it("Alien", 1979), _it("Weird Name", 2000, tvdb_id="5", type="series"),
           _it("Amélie", 2001), _it("Heat", 1995, tmdb_id="949"), _it("Extra Movie", 2020)]
    jf = [_it("Dune", 2021), _it("Alien", None), _it("Totally Different", 2000, tvdb_id="5", type="series"),
          _it("Amelie", 2001), _it("Heat (Director's Cut)", 1995, tmdb_id="949"), _it("Only On Jellyfin", 2019)]
    r = compare(nas, jf)
    assert sorted(i["title"] + str(i["year"]) for i in r["only_a"]) == ["Dune1984", "Extra Movie2020"]
    assert [i["title"] for i in r["only_b"]] == ["Only On Jellyfin"]
    assert r["matched_by_id"] == 2 and r["matched_by_title"] == 3       # tvdb "5" and tmdb "949" by id
    # conflicting ids stop a title match
    r2 = compare([_it("Alien", 1979, tmdb_id="348")], [_it("Alien", 1979, tmdb_id="999")])
    assert len(r2["only_a"]) == 1 and len(r2["only_b"]) == 1


def test_compare_api_and_csv(tmp_path):
    make_tree(tmp_path)
    s = Settings(db_path=str(tmp_path.parent / (tmp_path.name + "_db") / "t.db"), sync_on_start=False, browse_roots=["*"])
    with TestClient(create_app(s)) as c:
        c.post("/api/sources", json={"type": "local", "path": str(tmp_path / "Movies"), "label": "A", "mode": "library"})
        c.post("/api/sources", json={"type": "local", "path": str(tmp_path / "TV"), "label": "B", "mode": "library"})
        c.post("/api/sync")
        for _ in range(100):
            if not c.get("/api/sync/status").json()["running"]:
                break
            time.sleep(0.1)
        r = c.get("/api/compare", params={"a": "A", "b": "B"}).json()
        assert r["a_total"] == 3 and r["b_total"] == 1 and r["both"] == 0 and len(r["only_b"]) == 1
        body = c.get("/api/compare", params={"a": "A", "b": "B", "format": "csv"}).text
        assert "only in A" in body and "only in B" in body and "Breaking Bad" in body


# ---------------- TMDB identification + corrections ----------------
from mediafinder import identify


def fake_tmdb(tmp_path):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        path, q = req.url.path.replace("/3", "", 1), dict(req.url.params)
        assert q.get("api_key") == "KEY"
        if path == "/configuration":
            return httpx.Response(200, json={})
        if path == "/movie/603":
            return httpx.Response(200, json={"id": 603, "title": "The Matrix", "release_date": "1999-03-30",
                                             "external_ids": {"imdb_id": "tt0133093"}})
        if path == "/tv/1396":
            return httpx.Response(200, json={"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20",
                                             "external_ids": {"imdb_id": "tt0903747", "tvdb_id": 81189}})
        if path == "/find/81189":
            return httpx.Response(200, json={"tv_results": [{"id": 1396}], "movie_results": []})
        if path == "/search/movie":
            if q["query"].lower().startswith("scream"):
                return httpx.Response(200, json={"results": [{"id": 4, "title": "Scream", "release_date": "2022-01-11"}]})
            return httpx.Response(200, json={"results": []})
        if path == "/movie/4":
            return httpx.Response(200, json={"id": 4, "title": "Scream", "release_date": "2022-01-11",
                                             "external_ids": {"imdb_id": "tt11245972"}})
        if path == "/search/tv":
            if "bad" in q["query"].lower():
                return httpx.Response(200, json={"results": [{"id": 1396, "name": "Breaking Bad",
                                                              "first_air_date": "2008-01-20"}]})
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={})

    db_path = str(tmp_path / "tm.db")
    db.connect(db_path).close()
    return identify.Tmdb("KEY", db_path, httpx.Client(transport=httpx.MockTransport(handler))), calls


def test_tmdb_lookup_paths(tmp_path):
    tm, calls = fake_tmdb(tmp_path)
    row = lambda **kw: {"type": "movie", "title": "x", "year": None, "tmdb_id": None,
                        "imdb_id": None, "tvdb_id": None, **kw}
    assert tm.lookup(row(tmdb_id="603"))["title"] == "The Matrix"                  # by tmdb id
    hit = tm.lookup(row(type="series", tvdb_id="81189"))                            # tvdb id -> /find
    assert hit["title"] == "Breaking Bad" and hit["tvdb_id"] == "81189" and hit["type"] == "series"
    hit = tm.lookup(row(title="scream", year=2022))                                 # search, canonical casing
    assert (hit["title"], hit["year"], hit["imdb_id"]) == ("Scream", 2022, "tt11245972")
    hit = tm.lookup(row(title="Breaking Bad"))                                      # typed movie, really a series
    assert hit["type"] == "series" and hit["tmdb_id"] == "1396"
    assert tm.lookup(row(title="Zzzz Nothing")) is None
    n = len(calls)
    tm.lookup(row(tmdb_id="603"))
    assert len(calls) == n                                                          # served from cache


def test_identify_all_and_rescan_keeps_identification(tmp_path):
    tm, _ = fake_tmdb(tmp_path)
    conn = db.connect(str(tmp_path / "tm.db"))
    scan = lambda: [db.Item("n", "Movies", "movie", "scream", 2022, "/m/scream 2022.mkv"),
                    db.Item("n", "Movies", "movie", "Zzzz", None, "/m/zzzz.mkv"),
                    db.Item("n", "TV", "series", "Breaking Bad", None, "/tv/bb", tvdb_id="81189")]
    db.replace_source(conn, "n", scan())
    stats = identify.identify_all(conn, tm)
    assert stats == {"checked": 3, "matched": 2, "unmatched": 1}
    names = {r["raw_title"]: r for r in conn.execute("SELECT * FROM items")}
    assert names["scream"]["title"] == "Scream" and names["scream"]["match_state"] == "tmdb"
    assert names["Breaking Bad"]["tmdb_id"] == "1396" and names["Zzzz"]["match_state"] == "unmatched"
    r = db.replace_source(conn, "n", scan())                    # re-scan: nothing to change
    assert (r["added"], r["updated"], r["removed"]) == (0, 0, 0)
    assert conn.execute("SELECT title FROM items WHERE raw_title='scream'").fetchone()[0] == "Scream"
    assert identify.identify_all(conn, tm)["checked"] == 0      # nothing left to look up
    assert identify.match_counts(conn) == {"identified": 2, "edited": 0, "unmatched": 1, "pending": 0}


def test_overrides_survive_rescan_and_can_hide():
    conn = db.connect(":memory:")
    it = db.Item("n", "Movies", "series", "Wrong Show", 2001, "/m/wrong")
    junk = db.Item("n", "Movies", "movie", "Sample", None, "/m/sample")
    db.replace_source(conn, "n", [it, junk])
    conn.execute("INSERT INTO overrides (source,key,title,year,type,ignored) VALUES ('n',?,?,?,?,0)",
                 (it.key, "Right Movie", 2001, "movie"))
    conn.execute("INSERT INTO overrides (source,key,title,ignored) VALUES ('n',?,?,1)", (junk.key, "Sample"))
    conn.commit()
    r = db.replace_source(conn, "n", [it, junk])
    assert r["total"] == 1 and r["removed"] == 1
    row = conn.execute("SELECT * FROM items").fetchone()
    assert (row["title"], row["type"], row["match_state"], row["raw_title"]) == (
        "Right Movie", "movie", "manual", "Wrong Show")


def wait_sync(c):
    time.sleep(0.2)
    for _ in range(150):
        if not c.get("/api/sync/status").json()["running"]:
            return
        time.sleep(0.1)


def test_correction_and_tmdb_endpoints(tmp_path, monkeypatch):
    make_tree(tmp_path)
    cfg_dir = tmp_path.parent / (tmp_path.name + "_db")
    s = Settings(db_path=str(cfg_dir / "t.db"), sync_on_start=False, browse_roots=["*"])
    monkeypatch.setattr(identify.Tmdb, "check_key", lambda self: None)
    with TestClient(create_app(s)) as c:
        c.post("/api/sources", json={"type": "local", "path": str(tmp_path / "Movies"),
                                     "label": "M", "mode": "library"})
        c.post("/api/sync")
        wait_sync(c)
        items = c.get("/api/search", params={"q": "matrix"}).json()["items"]
        assert len(items) == 1 and items[0]["match_state"] == "name"
        iid = items[0]["id"]
        assert c.put(f"/api/items/{iid}", json={"title": "The Matrix Reloaded", "year": 1}).status_code == 400
        assert c.put(f"/api/items/{iid}", json={"title": "The Matrix Reloaded", "year": 2003,
                                                "tmdb_id": "604"}).status_code == 200
        got = c.get("/api/search", params={"q": "reloaded"}).json()["items"][0]
        assert (got["year"], got["tmdb_id"], got["match_state"]) == (2003, "604", "manual")
        assert c.get("/api/config").json()["identify"]["edited"] == 1
        c.post("/api/sync")
        wait_sync(c)
        assert c.get("/api/search", params={"q": "reloaded"}).json()["total"] == 1   # survived the re-scan
        ov = c.get("/api/overrides").json()
        assert len(ov) == 1 and ov[0]["title"] == "The Matrix Reloaded"
        assert c.delete(f"/api/overrides/{ov[0]['id']}").status_code == 200          # undo
        assert c.get("/api/search", params={"q": "matrix"}).json()["items"][0]["match_state"] == "name"
        assert c.put(f"/api/items/{iid}", json={"ignore": True}).json()["hidden"] is True   # hide junk
        assert c.get("/api/search", params={"q": "matrix"}).json()["total"] == 0
        # Jellyfin items can't be edited here
        dbc = db.connect(s.db_path)
        db.replace_source(dbc, "jellyfin", [db.Item("jellyfin", "Films", "movie", "Zorro", 1975,
                                                    match_state="jellyfin")])
        dbc.close()
        jid = c.get("/api/search", params={"q": "zorro"}).json()["items"][0]["id"]
        assert c.put(f"/api/items/{jid}", json={"title": "x"}).status_code == 400
        # TMDB key: saved encrypted, removable
        assert c.post("/api/tmdb", json={"api_key": "abc123secret"}).status_code == 200
        assert c.get("/api/config").json()["tmdb"] is True
        assert "abc123secret" not in (cfg_dir / "config.json").read_text()
        assert c.delete("/api/tmdb").status_code == 200 and c.get("/api/config").json()["tmdb"] is False
