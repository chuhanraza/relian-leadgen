import base64, os, sys, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("GROQ_API_KEY", "x")

from common import db as dbm  # noqa: E402
import eicma_campaign as ec  # noqa: E402
from common import gmail_client  # noqa: E402


class FakeQuery:
    """Mimics PostgREST: filters, order, range, and the hard 1,000-row response cap."""
    def __init__(self, rows): self.rows, self.lo, self.hi, self.f, self.o = rows, 0, 999, [], None
    def select(self, *a, **k): return self
    def eq(self, k, v): self.f.append((k, v)); return self
    def limit(self, n): self.hi = min(self.hi, self.lo + n - 1); return self
    def order(self, k): self.o = k; return self
    def range(self, lo, hi): self.lo, self.hi = lo, hi; return self
    def insert(self, row): self.rows.append(row); return self
    def execute(self):
        r = [x for x in self.rows if all(x.get(k) == v for k, v in self.f)]
        if self.o: r = sorted(r, key=lambda x: x[self.o])
        return mock.Mock(data=r[self.lo:min(self.hi, self.lo + 999) + 1])


class FakeDB:
    def __init__(self, rows): self.rows = rows
    def table(self, name): return FakeQuery(self.rows)


class Pagination(unittest.TestCase):
    def test_over_1000_rows_all_returned(self):
        rows = [{"id": i, "vertical": "v", "domain": f"d{i}.com"} for i in range(2503)]
        got = dbm.fetch_all(lambda: FakeDB(rows).table("outreach_leads").select("*").eq("vertical", "v"))
        self.assertEqual(len(got), 2503)
        self.assertEqual(len({r["id"] for r in got}), 2503)

    def test_exact_page_boundary(self):
        rows = [{"id": i} for i in range(2000)]
        self.assertEqual(len(dbm.fetch_all(lambda: FakeDB(rows).table("t").select("*"))), 2000)

    def test_dedupe_set_sees_domain_past_row_1000(self):
        rows = [{"id": i, "vertical": "v", "domain": f"d{i}.com"} for i in range(1500)]
        doms = {r["domain"] for r in dbm.fetch_all(lambda: FakeDB(rows).table("x").select("*").eq("vertical", "v"))}
        self.assertIn("d1400.com", doms)

    def test_insert_idempotent(self):
        rows = [{"id": i, "vertical": "v", "domain": f"d{i}.com"} for i in range(1200)]
        d = FakeDB(rows)
        new = {"vertical": "v", "domain": "d1100.com"}
        self.assertFalse(dbm.insert_lead_once(d, new))
        self.assertEqual(len(rows), 1200)
        fresh = {"vertical": "v", "domain": "new.com"}
        self.assertTrue(dbm.insert_lead_once(d, fresh))
        self.assertFalse(dbm.insert_lead_once(d, dict(fresh)))
        self.assertEqual(len(rows), 1201)


NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)


class Guards(unittest.TestCase):
    def c(self, **k): return {"email": "a@b.com", "suppressed": False, "last_sent_at": None, "cycle_number": 3, **k}
    def test_helpdesk(self): self.assertFalse(ec.is_eligible(self.c(email="support@x.com"), NOW, True))
    def test_gap(self): self.assertFalse(ec.is_eligible(self.c(last_sent_at="2026-10-10T00:00:00Z"), NOW, True))
    def test_already_wave4(self): self.assertFalse(ec.is_eligible(self.c(last_wave_design="wave_4"), NOW, True))
    def test_dup_address_already_wave4(self):
        rows = [self.c(id=1, last_wave_design="wave_4"), self.c(id=2)]
        self.assertEqual(ec.select_batch(rows, NOW, True)[1], 0)
    def test_cutoff(self):
        self.assertFalse(ec.is_eligible(self.c(), datetime(2026, 11, 2, tzinfo=timezone.utc), True))
    def test_eligible(self): self.assertTrue(ec.is_eligible(self.c(), NOW, True))
    def test_select_all_rows_over_1000(self):
        rows = [self.c(id=i, email=f"u{i}@x.com") for i in range(1500)]
        got = ec.fetch_contacts(FakeDB(rows))
        self.assertEqual(len(got), 1500)


class Wave4(unittest.TestCase):
    def test_template_and_subject(self):
        body, subject = ec.load_wave(4)
        self.assertEqual(subject, "meet at EICMA?")
        self.assertIn("{{GREETING}}", body)
        self.assertIn("cid:eicma-wave4-banner", body)
        self.assertIn("Hall 14", body)
        self.assertNotIn("sialkot", body.lower())
        self.assertNotIn("pakistan", body.lower())
        self.assertIn("{{GREETING}}", ec.load_plain_alt(4))

    def test_brand_names(self):
        cases = {"a@motorrad-schmidt.it": "Motorrad Schmidt", "a@gmail.com": None,
                 "a@riders.co.uk": "Riders", "a@bikerstore24.com": None,
                 "a@motoshopmilanoroma.it": None, "a@a-b-c-d.it": None,
                 "a@moto.it": "Moto", "bad": None, "a@x.it": None}
        for email, want in cases.items():
            self.assertEqual(ec.brand_from_email(email), want, email)

    def test_subject_builder(self):
        meta = ec.load_wave_meta(4)
        self.assertEqual(ec.build_subject(4, "a@ducati.it", meta), "Ducati — meet at EICMA?")
        self.assertEqual(ec.build_subject(4, "a@gmail.com", meta), "meet at EICMA?")

    def test_banner_registered_and_small(self):
        path, cid = ec.WAVE_INLINE_BANNERS[4]
        self.assertEqual(cid, "eicma-wave4-banner")
        self.assertTrue(path.exists())
        self.assertLess(path.stat().st_size, 200_000)

    def test_greeting(self):
        with mock.patch.object(ec, "generate", return_value="Marco"):
            self.assertEqual(ec.extract_first_name("Marco Rossi - Moto SRL"), "Marco")
        with mock.patch.object(ec, "generate", return_value="Moto Rossi SRL"):
            self.assertEqual(ec.extract_first_name("Moto Rossi SRL"), "there")
        with mock.patch.object(ec, "generate", return_value="There."):
            self.assertEqual(ec.extract_first_name("info"), "there")
        with mock.patch.object(ec, "generate", side_effect=RuntimeError):
            self.assertEqual(ec.extract_first_name("Marco"), "there")

    def _draft(self, **kw):
        captured = {}
        svc = mock.MagicMock()
        svc.users().drafts().create.side_effect = lambda userId, body: captured.update(b=body) or mock.Mock(execute=lambda: {"id": "d"})
        with mock.patch.object(gmail_client, "build", return_value=svc), mock.patch.object(gmail_client, "_credentials"):
            gmail_client.create_draft("a@b.com", "s", from_email="hm@relianmfg.com", **kw)
        import email
        return email.message_from_bytes(base64.urlsafe_b64decode(captured["b"]["message"]["raw"]))

    def test_mime_plain_only(self):
        msg = self._draft(body="Hi there,\n\nbody", is_html=False)
        self.assertEqual([p.get_content_type() for p in msg.walk() if not p.is_multipart()], ["text/plain"])

    def test_mime_wave4(self):
        html_t, _ = ec.load_wave(4)
        path, cid = ec.WAVE_INLINE_BANNERS[4]
        msg = self._draft(body=html_t.replace("{{GREETING}}", "Marco"), is_html=True,
                          inline_image_path=path, inline_image_cid=cid,
                          plain_alt=ec.load_plain_alt(4).replace("{{GREETING}}", "Marco"))
        leaves = [p for p in msg.walk() if not p.is_multipart()]
        self.assertEqual(sorted(p.get_content_type() for p in leaves), ["image/jpeg", "text/html", "text/plain"])
        self.assertIn("multipart/related", [p.get_content_type() for p in msg.walk()])
        img = [p for p in leaves if p.get_content_type() == "image/jpeg"]
        self.assertEqual(len(img), 1)
        self.assertEqual(img[0]["Content-ID"], "<eicma-wave4-banner>")
        htm = [p for p in leaves if p.get_content_type() == "text/html"][0].get_payload(decode=True).decode()
        self.assertIn("cid:eicma-wave4-banner", htm)
        self.assertNotIn("sialkot", htm.lower())
        self.assertIn("Hi Marco,", [p for p in leaves if p.get_content_type() == "text/plain"][0].get_payload(decode=True).decode())


class NoSialkot(unittest.TestCase):
    def test_grep(self):
        for d in ("config", "scripts", "assets"):
            for p in (ROOT / d).rglob("*"):
                if p.is_file() and p.suffix in {".py", ".html", ".json", ".txt", ".md"}:
                    self.assertNotIn("sialkot", p.read_text(errors="ignore").lower(), str(p))


if __name__ == "__main__":
    unittest.main()
