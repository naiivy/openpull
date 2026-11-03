"""Offline tests: a local server with Range support, refusals, and adapters fed with API-shaped fixtures."""
import hashlib, http.server, json, os, shutil, sys, tempfile, threading, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import openpull as op

PAYLOAD = os.urandom(3_000_000)
STATE = {"fail_once": True}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send_file(self, head):
        rng = self.headers.get("Range")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        body = PAYLOAD[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "application/pdf"); self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="report 2026.pdf"'); self.end_headers()
        if head: return
        if self.path == "/flaky.pdf" and STATE["fail_once"] and not rng:
            STATE["fail_once"] = False; self.wfile.write(body[:1_000_000]); self.wfile.flush(); self.connection.close(); return
        self.wfile.write(body)
    def route(self, head):
        if self.path in ("/file.pdf", "/flaky.pdf"): return self._send_file(head)
        if self.path == "/members/course.mp4": self.send_response(401); self.end_headers(); return
        if self.path == "/premium.zip": self.send_response(402); self.end_headers(); return
        if self.path == "/page": self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); head or self.wfile.write(b"<html></html>"); return
        self.send_response(404); self.end_headers()
    def do_GET(self): self.route(False)
    def do_HEAD(self): self.route(True)


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
    def setUp(self): self.out = Path(tempfile.mkdtemp())
    def tearDown(self): shutil.rmtree(self.out)

    def test_direct_download_and_name(self):
        it = op.resolve(self.base + "/file.pdf")[0]
        self.assertEqual(it.name, "report 2026.pdf"); self.assertEqual(it.size, len(PAYLOAD))
        res = op.download(it, self.out)
        self.assertEqual((self.out / it.name).read_bytes(), PAYLOAD); self.assertEqual(res["check"], "size ok")

    def test_resume_after_dropped_connection(self):
        it = op.resolve(self.base + "/flaky.pdf")[0]; it.sha256 = hashlib.sha256(PAYLOAD).hexdigest()
        res = op.download(it, self.out)
        self.assertEqual(res["status"], "resumed"); self.assertEqual(res["check"], "sha256 ok")

    def test_checksum_mismatch_is_caught(self):
        it = op.resolve(self.base + "/file.pdf")[0]; it.md5 = "0" * 32
        with self.assertRaises(ValueError): op.download(it, self.out)

    def test_refuses_login_payment_pages_and_streams(self):
        for path in ("/members/course.mp4", "/premium.zip", "/page"):
            with self.assertRaises(op.Refused): op.resolve(self.base + path)
        with self.assertRaises(op.Refused): op.resolve("https://example.com/video/master.m3u8")

    def test_cli_writes_credits(self):
        code = op.main([self.base + "/file.pdf", self.base + "/premium.zip", "-o", str(self.out)])
        self.assertEqual(code, 0)
        rows = json.loads((self.out / "credits.json").read_text())
        self.assertEqual(len(rows), 1); self.assertTrue((self.out / "CREDITS.md").exists())

    # adapters, with responses shaped like the real public APIs
    def fake(self, mapping):
        orig = op.get_json
        op.get_json = lambda url: next(v for k, v in mapping.items() if k in url)
        self.addCleanup(lambda: setattr(op, "get_json", orig))

    def test_internet_archive(self):
        self.fake({"archive.org/metadata/night_of_the_living_dead": {"metadata": {"title": "Night of the Living Dead", "licenseurl": "http://creativecommons.org/publicdomain/mark/1.0/"},
            "files": [{"name": "night.mp4", "size": "123", "md5": "a" * 32, "source": "derivative"}, {"name": "night_meta.xml", "source": "metadata"}]}})
        items = op.resolve("https://archive.org/details/night_of_the_living_dead")
        self.assertEqual([i.name for i in items], ["night.mp4"]); self.assertEqual(items[0].md5, "a" * 32)
        self.assertIn("publicdomain", items[0].license)

    def test_internet_archive_lending_is_refused(self):
        self.fake({"archive.org/metadata/": {"metadata": {"access-restricted-item": "true"}, "files": []}})
        with self.assertRaises(op.Refused): op.resolve("https://archive.org/details/somebook")

    def test_wikimedia(self):
        self.fake({"commons.wikimedia.org": {"query": {"pages": {"1": {"imageinfo": [{"url": "https://upload.wikimedia.org/a/ab/Earthrise.jpg", "sha1": "b" * 40, "size": 99,
            "extmetadata": {"LicenseShortName": {"value": "Public domain"}, "Artist": {"value": "<a href='x'>William Anders</a>"}}}]}}}}})
        it = op.resolve("https://commons.wikimedia.org/wiki/File:NASA-Apollo8-Dec24-Earthrise.jpg")[0]
        self.assertEqual(it.license, "Public domain"); self.assertEqual(it.author, "William Anders"); self.assertEqual(it.name, "Earthrise.jpg")

    def test_nasa(self):
        self.fake({"/asset/": {"collection": {"items": [{"href": "http://images-assets.nasa.gov/image/as11-40-5874/as11-40-5874~orig.jpg"},
                                                        {"href": "http://images-assets.nasa.gov/image/as11-40-5874/as11-40-5874~thumb.jpg"}]}},
                   "/search": {"collection": {"items": [{"data": [{"title": "Apollo 11 Mission image"}]}]}}})
        items = op.resolve("https://images.nasa.gov/details/as11-40-5874")
        self.assertEqual(len(items), 1); self.assertTrue(items[0].url.startswith("https://")); self.assertIn("~orig", items[0].name)

    def test_arxiv(self):
        it = op.resolve("https://arxiv.org/abs/1706.03762")[0]
        self.assertEqual(it.url, "https://arxiv.org/pdf/1706.03762"); self.assertEqual(it.name, "1706.03762.pdf")

    def test_github(self):
        self.fake({"/releases/latest": {"tag_name": "v2.0", "assets": [{"name": "tool-linux.tar.gz", "browser_download_url": "https://github.com/o/r/releases/download/v2.0/tool-linux.tar.gz", "size": 5, "digest": "sha256:" + "c" * 64}]},
                   "api.github.com/repos/o/r": {"license": {"spdx_id": "MIT"}}})
        it = op.resolve("https://github.com/o/r")[0]
        self.assertEqual(it.sha256, "c" * 64); self.assertEqual(it.license, "MIT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
