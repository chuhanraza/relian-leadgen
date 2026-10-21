"""One-off: drafts 4 banner embedding variants to hm@relianmfg.com so we can see which
one Gmail renders. Drafts only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.gmail_client import create_draft
from eicma_campaign import BRANDING_DIR, FROM_EMAIL

IMG = BRANDING_DIR / "eicma_wave5_banner.jpg"
URL = "https://raw.githubusercontent.com/chuhanraza/relian-leadgen/main/assets/branding/eicma_wave5_banner.jpg"
TO = "hm@relianmfg.com"
def page(img): return f"<p>Hi Team,</p><p>Banner test.</p><p>{img}</p><p>Hamad</p>"

create_draft(TO, "TEST A flat cid", page('<img src="cid:t" width="600" alt="banner" style="display:block;width:100%;max-width:600px;height:auto">'),
             from_email=FROM_EMAIL, is_html=True, inline_image_path=IMG, inline_image_cid="t")
create_draft(TO, "TEST B flat cid plain img", page('<img src="cid:t" width="600" alt="banner">'),
             from_email=FROM_EMAIL, is_html=True, inline_image_path=IMG, inline_image_cid="t")
create_draft(TO, "TEST C github url", page(f'<img src="{URL}" width="600" alt="banner" style="display:block;max-width:100%;height:auto">'),
             from_email=FROM_EMAIL, is_html=True)
create_draft(TO, "TEST D attachment only", page("see attached"), attachment_paths=[IMG],
             from_email=FROM_EMAIL, is_html=True)
print("4 test drafts created")
