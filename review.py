#!/usr/bin/env python3
"""
python review.py /path/to/Takeout /path/to/quarantine
Then open http://127.0.0.1:8765
Reads flags.csv (from `photo_cleanup.py report`).
"""
import csv, hashlib, json, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

ROOT = Path(sys.argv[1]).resolve()
QUAR = Path(sys.argv[2]).resolve()
CSV = "flags.csv"
CACHE = Path(".thumbs")
CACHE.mkdir(exist_ok=True)
LOCK = threading.Lock()


def pid(p):
    return hashlib.md5(p.encode()).hexdigest()[:16]


def load_items():
    out = []
    for r in csv.DictReader(open(CSV)):
        if Path(r["path"]).exists():
            k = r["kept_instead"]
            out.append({"id": pid(r["path"]), "path": r["path"],
                        "reason": r["reason"], "keep": k,
                        "keep_id": pid(k) if k else ""})
    return out


ITEMS = load_items()
PATHS = {}


def rebuild():
    PATHS.clear()
    for it in ITEMS:
        PATHS[it["id"]] = it["path"]
        if it["keep"]:
            PATHS[it["keep_id"]] = it["keep"]


rebuild()


def save():
    with open(CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "reason", "kept_instead"])
        for it in ITEMS:
            w.writerow([it["path"], it["reason"], it["keep"]])


def thumb(i, size):
    size = max(100, min(size, 2000))
    f = CACHE / f"{i}_{size}.jpg"
    if not f.exists():
        im = ImageOps.exif_transpose(Image.open(PATHS[i])).convert("RGB")
        im.thumbnail((size, size))
        im.save(f, "JPEG", quality=80)
    return f.read_bytes()


def apply(ids):
    global ITEMS
    with LOCK:
        sel = set(ids)
        # never move a file that is the designated keeper of a selected file
        keepers = {it["keep"] for it in ITEMS if it["id"] in sel and it["keep"]}
        done, skipped = [], 0
        for it in ITEMS:
            if it["id"] not in sel:
                continue
            if it["path"] in keepers:
                skipped += 1
                continue
            try:
                src = Path(it["path"])
                dst = QUAR / src.resolve().relative_to(ROOT)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                done.append(it["id"])
            except Exception as e:
                print("failed", it["path"], e)
                skipped += 1
        ITEMS = [it for it in ITEMS if it["id"] not in set(done)]
        save()
        rebuild()
        return {"done": done, "skipped": skipped}


PAGE = """<!doctype html><meta charset=utf-8><title>Photo review</title>
<style>
body{font-family:system-ui;margin:0;background:#111;color:#eee}
header{position:sticky;top:0;background:#222;padding:10px;display:flex;
  gap:8px;flex-wrap:wrap;align-items:center;z-index:2}
button{padding:6px 12px;cursor:pointer}
button.on{background:#fc0}
#apply{background:#c33;color:#fff;border:0;margin-left:auto}
.grid{display:flex;flex-wrap:wrap;gap:8px;padding:10px}
.card{background:#222;border:2px solid #a44;padding:4px;width:210px}
.card.keep{border-color:#4a4}
.card img{width:100%;height:150px;object-fit:contain;background:#000}
small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style>
<header>
  <span id=tabs></span>
  <button onclick="selAll(true)">select all</button>
  <button onclick="selAll(false)">select none</button>
  <span id=count></span>
  <button id=apply onclick="doApply()">Move selected to quarantine</button>
</header>
<div class=grid id=grid></div>
<script>
let items=[], tab=null, shown=100, checked=new Set();
const esc=s=>s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function init(){
  items=await (await fetch('/api/items')).json();
  items.forEach(i=>checked.add(i.id));
  render();
}
const reasons=()=>[...new Set(items.map(i=>i.reason))];
function setTab(r){tab=r;shown=100;render();}
function card(it){
  const c=checked.has(it.id);
  const big=id=>`/thumb?i=${id}&s=1600`, sm=id=>`/thumb?i=${id}`;
  return `<div class="card ${c?'':'keep'}">
    <label><input type=checkbox ${c?'checked':''}
      onchange="tog('${it.id}',this)"> delete</label>
    <a href="${big(it.id)}" target=_blank><img loading=lazy src="${sm(it.id)}"></a>
    ${it.keep_id?`<div>keeps instead:</div>
      <a href="${big(it.keep_id)}" target=_blank><img loading=lazy src="${sm(it.keep_id)}"></a>`:''}
    <small>${esc(it.name)}</small></div>`;
}
function render(){
  const rs=reasons();
  if(!rs.includes(tab)) tab=rs[0];
  document.getElementById('tabs').innerHTML=rs.map(r=>
    `<button class="${r==tab?'on':''}" onclick="setTab('${r}')">${r} (${items.filter(i=>i.reason==r).length})</button>`).join('');
  const list=items.filter(i=>i.reason==tab);
  document.getElementById('grid').innerHTML=list.slice(0,shown).map(card).join('')
    +(list.length>shown?`<button onclick="shown+=100;render()">show more (${list.length-shown})</button>`:'');
  count();
}
function count(){document.getElementById('count').textContent=checked.size+' selected';}
function tog(id,el){
  el.checked?checked.add(id):checked.delete(id);
  el.closest('.card').classList.toggle('keep',!el.checked);
  count();
}
function selAll(v){
  items.filter(i=>i.reason==tab).forEach(i=>v?checked.add(i.id):checked.delete(i.id));
  render();
}
async function doApply(){
  if(!checked.size||!confirm(`Move ${checked.size} files to quarantine?`)) return;
  const r=await (await fetch('/apply',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids:[...checked]})})).json();
  const d=new Set(r.done);
  items=items.filter(i=>!d.has(i.id));
  d.forEach(id=>checked.delete(id));
  render();
  alert(`Moved ${r.done.length}. Skipped ${r.skipped}.`);
}
init();
</script>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif u.path == "/api/items":
            data = [{"id": it["id"], "reason": it["reason"],
                     "keep_id": it["keep_id"], "name": Path(it["path"]).name}
                    for it in ITEMS]
            self._send(200, json.dumps(data).encode(), "application/json")
        elif u.path == "/thumb":
            try:
                body = thumb(q["i"][0], int(q.get("s", ["400"])[0]))
                self._send(200, body, "image/jpeg")
            except Exception:
                self._send(404, b"", "text/plain")
        else:
            self._send(404, b"", "text/plain")

    def do_POST(self):
        if (self.path != "/apply"
                or self.headers.get("Content-Type") != "application/json"):
            return self._send(400, b"", "text/plain")
        n = int(self.headers["Content-Length"])
        ids = json.loads(self.rfile.read(n))["ids"]
        self._send(200, json.dumps(apply(ids)).encode(), "application/json")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"{len(ITEMS)} flagged files. Open http://127.0.0.1:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()