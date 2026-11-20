import json
import threading
from contextlib import closing
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bibliotheque_mtg import local_app
from bibliotheque_mtg.proxies import Proxies


def test_proxy_deletion_requires_confirmation_and_removes_local_image(tmp_path, monkeypatch):
    folder = tmp_path / "proxy"
    folder.mkdir()
    image = folder / "Proxy.png"
    image.write_bytes(b"image")
    other = tmp_path / "Other.png"
    other.write_bytes(b"other")
    proxies = Proxies(folder, tmp_path / "state")
    proxies.state.mkdir()
    provider_id = next(iter(proxies.files()))
    with closing(proxies.associations()) as conn, conn:
        conn.execute("CREATE TABLE associations (id TEXT PRIMARY KEY, source_id TEXT NOT NULL)")
        conn.execute("INSERT INTO associations VALUES (?, 'printing')", (provider_id,))
    monkeypatch.setattr(local_app, "PROXIES", proxies)

    with local_app.LocalServer(("127.0.0.1", 0), local_app.LibraryHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"

        def delete(identifier, confirmed, request_origin=origin):
            headers = {"Content-Type": "application/json"}
            if request_origin is not None:
                headers["Origin"] = request_origin
            body = json.dumps({"provider_card_id": identifier, "confirmed": confirmed}).encode()
            request = Request(origin + "/api/v2/proxies/delete", data=body, headers=headers)
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)

        try:
            assert delete(provider_id, True, None)[0] == 403
            assert delete(provider_id, True, "https://example.test")[0] == 403
            assert delete(provider_id, False)[0] == 400
            assert delete("../Other.png", True)[0] == 404
            assert image.is_file()
            with closing(proxies.associations()) as conn:
                assert conn.execute("SELECT source_id FROM associations").fetchone() == ("printing",)

            status, result = delete(provider_id, True)
            assert status == 200
            assert result == {"deleted": True, "provider_card_id": provider_id}
            assert not image.exists()
            assert not proxies.files()
            assert other.read_bytes() == b"other"
            with closing(proxies.associations()) as conn:
                assert conn.execute("SELECT source_id FROM associations").fetchone() is None
            assert delete(provider_id, True)[0] == 404
        finally:
            server.shutdown()
            thread.join()
