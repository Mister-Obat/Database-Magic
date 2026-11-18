import gzip
import hashlib
import io
import json
import tempfile
import unittest
import datetime
from pathlib import Path
from unittest.mock import Mock

import requests
from tests_bibliotheque.test_support import find_ops_root

from bibliotheque_mtg.sources import (
    ArchiveIntegrityError,
    HttpClient,
    PaginationError,
    SourceBlocked,
    SourceFormatError,
    SourceHTTPError,
    SourceRateLimited,
    SourceTransportError,
    all_cards_descriptor,
    build_catalogue_db,
    download_bulk,
    extract_official_card_assets,
    fetch_cached,
    gallery_article_links,
    gallery_product_archive_entries,
    gallery_product_archive_links,
    iter_api_pages,
    rebuild_from_archives,
    validate_gzip_jsonl,
)


def response(status=200, body=b"{}", headers=None, url="https://fixture.invalid/data"):
    obj = requests.Response()
    obj.status_code = status
    obj._content = body
    obj.headers.update(headers or {})
    obj.url = url
    return obj


def gzip_jsonl(records):
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb") as stream:
        for row in records:
            stream.write(json.dumps(row).encode("utf-8") + b"\n")
    return output.getvalue()


class FakeSession:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.max_redirects = 3

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakePageClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        payload = self.payloads.pop(0)
        obj = response(body=json.dumps(payload).encode())
        obj.json = lambda: payload
        return obj


class SourceTests(unittest.TestCase):
    def minimal_config(self):
        return {"scope": {"explicit_exclusions": [], "include_unreleased": False,
                           "include_digital": True, "include_paper": True,
                           "include_promos": True, "include_tokens": True}}

    def test_gzip_jsonl_validates_rows_and_languages(self):
        body = gzip_jsonl([
            {"object": "card", "id": "a", "lang": "fr"},
            {"object": "card", "id": "b", "lang": "en"},
            {"object": "card", "id": "c", "lang": "ja"},
        ])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.jsonl.gz"
            path.write_bytes(body)
            self.assertEqual({"rows": 3, "languages": {"en": 1, "fr": 1, "ja": 1}}, validate_gzip_jsonl(path))

    def test_invalid_jsonl_gzip_truncation_and_html_200_are_rejected(self):
        cases = [
            b"not gzip",
            gzip_jsonl([{"object": "card", "id": "a", "lang": "fr"}])[:-5],
            gzip.compress(b"<html>proxy error</html>"),
            gzip.compress(b'{"object":"card","id":"a","lang":"fr"}\nnot-json\n'),
        ]
        with tempfile.TemporaryDirectory() as folder:
            for i, body in enumerate(cases):
                path = Path(folder) / f"bad-{i}.gz"
                path.write_bytes(body)
                with self.subTest(i=i), self.assertRaises(ArchiveIntegrityError):
                    validate_gzip_jsonl(path)

    def test_descriptor_requires_current_jsonl_download_uri(self):
        old = {"data": [{"type": "all_cards", "download_uri": "https://example.invalid/old.tar"}]}
        with self.assertRaises(SourceFormatError):
            all_cards_descriptor(old)
        current = {"data": [{"type": "all_cards", "updated_at": "2026-10-20T00:00:00Z",
                             "compressed_size": 1,
                             "jsonl_download_uri": "https://data.scryfall.io/all-cards/test.jsonl.gz"}]}
        self.assertEqual("all_cards", all_cards_descriptor(current)["type"])

    def test_json_endpoint_rejects_html_even_with_http_200(self):
        client = HttpClient("Fixture/1.0", session=FakeSession([response(
            body=b"<html>blocked</html>", headers={"Content-Type": "text/html"})]),
            sleep=lambda _: None, min_intervals={"fixture.invalid": 0})
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(SourceFormatError):
                fetch_cached(client, "https://fixture.invalid/data", Path(folder) / "body.json", expected="json")

    def test_metadata_http_403_is_not_retried_or_called_absence(self):
        session = FakeSession([response(403)])
        client = HttpClient("Fixture/1.0", session=session, sleep=lambda _: None,
                            min_intervals={"fixture.invalid": 0})
        with self.assertRaises(SourceBlocked):
            client.get("https://fixture.invalid/data")
        self.assertEqual(1, len(session.calls))

    def test_http_429_honors_retry_after_and_large_wait_is_deferred(self):
        sleep = Mock()
        session = FakeSession([response(429, headers={"Retry-After": "2"}), response()])
        client = HttpClient("Fixture/1.0", session=session, sleep=sleep,
                            min_intervals={"fixture.invalid": 0})
        self.assertEqual(200, client.get("https://fixture.invalid/data").status_code)
        sleep.assert_any_call(2.0)
        delayed = HttpClient("Fixture/1.0", session=FakeSession([response(429, headers={"Retry-After": "3600"})]),
                             sleep=lambda _: self.fail("long Retry-After must defer"),
                             min_intervals={"fixture.invalid": 0})
        with self.assertRaises(SourceRateLimited) as raised:
            delayed.get("https://fixture.invalid/data")
        self.assertEqual(3600.0, raised.exception.retry_after)

    def test_timeout_and_server_errors_have_bounded_retries(self):
        timeouts = FakeSession([requests.Timeout("slow")] * 3)
        client = HttpClient("Fixture/1.0", session=timeouts, sleep=lambda _: None,
                            min_intervals={"fixture.invalid": 0})
        with self.assertRaises(SourceTransportError):
            client.get("https://fixture.invalid/data")
        self.assertEqual(3, len(timeouts.calls))
        server = FakeSession([response(503), response(502), response(500)])
        client = HttpClient("Fixture/1.0", session=server, sleep=lambda _: None,
                            min_intervals={"fixture.invalid": 0})
        with self.assertRaises(SourceHTTPError):
            client.get("https://fixture.invalid/data")
        self.assertEqual(3, len(server.calls))

    def test_pagination_two_pages_finishes_and_repeated_page_fails(self):
        page1 = {"data": [{"id": "1"}], "has_more": True, "next_page": "https://fixture.invalid/sets?page=2"}
        page2 = {"data": [{"id": "2"}], "has_more": False, "next_page": None}
        client = FakePageClient([page1, page2])
        pages = list(iter_api_pages(client, "https://fixture.invalid/sets"))
        self.assertEqual(2, len(pages))
        self.assertEqual(2, len(client.calls))
        repeated = FakePageClient([page1, {"data": [{"id": "1"}], "has_more": True,
                                   "next_page": "https://fixture.invalid/sets?page=3"}])
        with self.assertRaises(PaginationError):
            list(iter_api_pages(repeated, "https://fixture.invalid/sets"))

    def test_missing_next_page_fails_instead_of_successful_empty_catalogue(self):
        client = FakePageClient([{"data": [], "has_more": True}])
        with self.assertRaises(SourceFormatError):
            list(iter_api_pages(client, "https://fixture.invalid/sets"))

    def test_conditional_cache_reuses_validated_body_after_304(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sets.json"
            path.write_bytes(b'{"data":[],"has_more":false}')
            path.with_name(path.name + ".meta.json").write_text(json.dumps({"etag": "abc", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
            session = FakeSession([response(304)])
            client = HttpClient("Fixture/1.0", session=session, sleep=lambda _: None,
                                min_intervals={"fixture.invalid": 0})
            body, meta, fetched = fetch_cached(client, "https://fixture.invalid/sets", path, expected="json")
            self.assertFalse(fetched)
            self.assertEqual(b'{"data":[],"has_more":false}', body)
            self.assertEqual("abc", session.calls[0][1]["headers"]["If-None-Match"])

    def test_wizards_gallery_extraction_records_links_and_verified_asset_language(self):
        listing = b'''<html><a href="/fr/news/card-image-gallery/magicorigins">Origins</a>
        <a href="/fr/products/mh3/card-image-gallery">MH3</a>
        <a href="/fr/news/card-image-gallery">index</a></html>'''
        links = gallery_article_links(listing, "https://magic.wizards.com/fr/news/card-image-gallery?page=23")
        self.assertEqual(2, len(links))
        article = b'''<html><img src="http://media.wizards.com/2015/x/fr_abc.png" alt="Carte FR">
        <img src="http://media.wizards.com/2015/x/en_xyz.png" alt="Card EN">
        <img src="https://images.ctfassets.net/logo.png" alt="logo"></html>'''
        assets = extract_official_card_assets(article, "https://magic.wizards.com/fr/news/card-image-gallery/x")
        self.assertEqual(["en", "fr"], [item["language"] for item in assets])

    def test_wizards_product_archive_discovers_encoded_french_gallery_urls(self):
        archive = b'''<html><nav><a href="/fr/products/nav-promo">Menu link</a></nav>
        <a href="/fr/products/modern-horizons-3/card-image-gallery">MH3</a>
        <a href="/fr/products/modern-horizons-3">Horizons du Modern 3</a>
        <a href="/fr/products/marvel/secret-lair">Secret Lair</a>
        <a href="/fr/products/card-set-archive">Archive</a>
        <script>"https:\\u002F\\u002Fmagic.wizards.com\\u002Ffr\\u002Fproducts\\u002Fset-abc\\u002Fcard-image-gallery"</script>
        <a href="/en/products/english-set">English</a>
        <a href="https://magic.wizards.com/en/products/not-french/card-image-gallery">EN</a>
        <a href="https://example.invalid/fr/products/nope/card-image-gallery">external</a></html>'''
        links = gallery_product_archive_links(archive)
        self.assertEqual([
            "https://magic.wizards.com/fr/products/modern-horizons-3/card-image-gallery",
            "https://magic.wizards.com/fr/products/set-abc/card-image-gallery",
        ], [row["url"] for row in links])
        self.assertTrue(all(row["discovered_from"] == "product_archive" for row in links))
        products = gallery_product_archive_entries(archive)["product_pages"]
        self.assertEqual([
            {"url": "https://magic.wizards.com/fr/products/marvel/secret-lair", "title": "Secret Lair"},
            {"url": "https://magic.wizards.com/fr/products/modern-horizons-3", "title": "Horizons du Modern 3"},
        ], products)

    def test_bulk_archive_reuse_does_not_call_network_when_version_and_hash_match(self):
        body = gzip_jsonl([{"object": "card", "id": "a", "lang": "fr"}])
        descriptor = {"jsonl_download_uri": "https://data.scryfall.io/all-cards/test.jsonl.gz",
                      "updated_at": "2026-10-20T00:00:00Z", "compressed_size": len(body)}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "test.jsonl.gz"
            archive.write_bytes(body)
            meta = {"updated_at": descriptor["updated_at"], "sha256": hashlib.sha256(body).hexdigest(),
                    "validation": {"rows": 1, "languages": {"fr": 1}}}
            archive.with_name(archive.name + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")

            class NoNetwork:
                def get(self, *_args, **_kwargs):
                    raise AssertionError("unchanged bulk archive must be reused")

            path, result, downloaded = download_bulk(NoNetwork(), descriptor, root)
            self.assertEqual(archive, path)
            self.assertFalse(downloaded)
            self.assertTrue(result["reused"])

    def test_bulk_download_promotes_valid_gzip_and_discards_invalid_partial(self):
        body = gzip_jsonl([{"object": "card", "id": "a", "lang": "fr"}])
        descriptor = {"jsonl_download_uri": "https://data.scryfall.io/all-cards/test.jsonl.gz",
                      "updated_at": "2026-10-20T00:00:00Z", "compressed_size": len(body)}

        class StreamResponse:
            def __init__(self, content):
                self.content = content
            def iter_content(self, chunk_size):
                for offset in range(0, len(self.content), max(1, chunk_size // 2)):
                    yield self.content[offset:offset + max(1, chunk_size // 2)]

        class DownloadClient:
            def __init__(self, content): self.content = content
            def get(self, *_args, **_kwargs): return StreamResponse(self.content)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path, meta, downloaded = download_bulk(DownloadClient(body), descriptor, root)
            self.assertTrue(downloaded)
            self.assertEqual(1, meta["validation"]["rows"])
            self.assertTrue(path.is_file())
            self.assertFalse(path.with_name(path.name + ".part").exists())
        bad_descriptor = {**descriptor, "compressed_size": len(body)}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ArchiveIntegrityError):
                download_bulk(DownloadClient(b"not gzip"), bad_descriptor, root)
            self.assertFalse((root / "test.jsonl.gz").exists())
            self.assertFalse((root / "test.jsonl.gz.part").exists())

    def test_sqlite_import_reconciles_all_languages_and_keeps_only_fr_en_rows(self):
        records = [
            {"object": "card", "id": "en-1", "oracle_id": "oracle-1", "lang": "en", "set_id": "set-1",
             "set": "syn", "collector_number": "123a", "name": "Test", "cmc": 0},
            {"object": "card", "id": "fr-1", "oracle_id": "oracle-1", "lang": "fr", "set_id": "set-1",
             "set": "syn", "collector_number": "123a", "name": "Essai", "cmc": 0},
            {"object": "card", "id": "ja-1", "oracle_id": "oracle-1", "lang": "ja", "set_id": "set-1",
             "set": "syn", "collector_number": "123a", "name": "テスト"},
        ]
        body = gzip_jsonl(records)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "cards.jsonl.gz"; archive.write_bytes(body)
            set_item = {"id": "set-1", "code": "syn", "name": "Synthetic", "set_type": "expansion",
                        "released_at": "2020-01-01", "digital": False, "card_count": 1}
            meta = {"updated_at": "2026-10-20T00:00:00Z", "sha256": hashlib.sha256(body).hexdigest()}
            scope = build_catalogue_db(archive, [set_item], root / "catalogue.sqlite", self.minimal_config(),
                                       meta, "b" * 64, datetime.date(2026, 10, 20))
            import sqlite3
            connection = sqlite3.connect(root / "catalogue.sqlite")
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
            self.assertEqual("123a", connection.execute("SELECT collector_number FROM cards WHERE id='fr-1'").fetchone()[0])
            self.assertEqual(3, connection.execute("SELECT SUM(record_count) FROM language_counts").fetchone()[0])
            connection.close()
            self.assertEqual(3, scope["card_lines_total"])
            self.assertEqual(2, scope["normalized_fr_en_records"])
            self.assertEqual(1, scope["other_language_records_archived_only"])
            self.assertEqual(0, scope["unmatched_set_id_references"])

    def test_offline_rebuild_reads_verified_archives_without_http(self):
        records = [
            {"object": "card", "id": "en-1", "lang": "en", "set_id": "set-1", "set": "syn",
             "collector_number": "1", "name": "Test"},
            {"object": "card", "id": "fr-1", "lang": "fr", "set_id": "set-1", "set": "syn",
             "collector_number": "1", "name": "Essai"},
        ]
        body = gzip_jsonl(records)
        with tempfile.TemporaryDirectory() as folder:
            ops = Path(folder); (ops / "schemas").mkdir(); (ops / "reports").mkdir()
            project = ops / "project"; project.mkdir()
            library = project / "Cartes Magic"
            config_path = ops / "config.json"
            actual_ops = find_ops_root(__file__)
            config = json.loads((actual_ops / "config.example.json").read_text(encoding="utf-8-sig"))
            config["paths"].update({"project_root": str(project), "library_root": str(library),
                                    "ops_root": str(ops), "staging_root": str(ops / "staging")})
            config_path.write_text(json.dumps(config), encoding="utf-8")
            schema = (actual_ops / "schemas" / "config.schema.json").read_bytes()
            (ops / "schemas" / "config.schema.json").write_bytes(schema)
            bulk_root = ops / "sources" / "scryfall" / "bulk"; bulk_root.mkdir(parents=True)
            descriptor = {"data": [{"type": "all_cards", "updated_at": "2026-10-20T00:00:00Z",
                                    "compressed_size": len(body),
                                    "jsonl_download_uri": "https://data.scryfall.io/all-cards/test.jsonl.gz"}]}
            (bulk_root / "bulk-data.json").write_text(json.dumps(descriptor), encoding="utf-8")
            archive = bulk_root / "test.jsonl.gz"; archive.write_bytes(body)
            bulk_meta = {"updated_at": descriptor["data"][0]["updated_at"],
                         "sha256": hashlib.sha256(body).hexdigest(), "validation": {"rows": 2, "languages": {"en": 1, "fr": 1}}}
            archive.with_name(archive.name + ".meta.json").write_text(json.dumps(bulk_meta), encoding="utf-8")
            sets_root = ops / "sources" / "scryfall" / "sets"; sets_root.mkdir(parents=True)
            page = {"data": [{"id": "set-1", "code": "syn", "name": "Synthetic", "set_type": "expansion",
                              "released_at": "2020-01-01", "digital": False, "card_count": 1}], "has_more": False}
            raw_page = json.dumps(page).encode()
            set_path = sets_root / "sets-page-0001.json"; set_path.write_bytes(raw_page)
            set_meta = {"sha256": hashlib.sha256(raw_page).hexdigest()}
            set_path.with_name(set_path.name + ".meta.json").write_text(json.dumps(set_meta), encoding="utf-8")
            scope = rebuild_from_archives(config_path, as_of=datetime.date(2026, 10, 20))
            self.assertEqual(1, scope["sets_eligible"])
            self.assertEqual(2, scope["normalized_fr_en_records"])
            self.assertTrue((ops / "catalogue.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
