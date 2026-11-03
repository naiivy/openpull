# OpenPull

Paste a link. Get the file that's already free, verified and credited.

OpenPull downloads files that are openly available on the web: public archives, public-domain media, open papers and release builds. It does **not** log in, bypass paywalls, strip DRM, rip streaming services or borrow lending-library books. If a server asks for a login or payment, it stops.

## Sources

| Source | Paste a link like | What you get |
|---|---|---|
| Internet Archive | `https://archive.org/details/<id>` | every file in the item, MD5-checked (lending items are refused) |
| Wikimedia Commons | `https://commons.wikimedia.org/wiki/File:<name>` | the full-size original, license and author |
| NASA Image Library | `https://images.nasa.gov/details/<nasa_id>` | the original asset |
| arXiv | `https://arxiv.org/abs/<id>` | the paper's PDF |
| GitHub releases | `https://github.com/<owner>/<repo>` | latest release assets, SHA-256 checked when GitHub publishes a digest |
| Any direct link | `https://…/report.pdf` | the file itself (web pages and HLS/DASH stream manifests are refused) |

## Use

```bash
python3 openpull.py https://arxiv.org/abs/1706.03762
python3 openpull.py -f links.txt --only pdf,mp4 -o library --workers 4
python3 openpull.py https://archive.org/details/<id> --dry-run
```

- downloads 4 files at once, retries with backoff
- resumes from the saved `.part` file when a connection drops
- verifies SHA-256 / SHA-1 / MD5 when the source publishes one, otherwise the size
- writes `credits.json` and `CREDITS.md` with source, author and license for every file

Python 3.9+, standard library only. Run the tests with `python3 test_openpull.py`.

Before you reuse anything, check its license in `CREDITS.md`. "Openly available" does not always mean "free to republish".

MIT licensed.
