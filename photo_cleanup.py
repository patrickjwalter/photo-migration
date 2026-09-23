#!/usr/bin/env python3
"""
Local photo cleanup. Nothing leaves your machine.

  python photo_cleanup.py scan   /path/to/Takeout
  python photo_cleanup.py report
  python photo_cleanup.py apply  /path/to/Takeout /path/to/quarantine
"""
import argparse, csv, hashlib, html, shutil, sqlite3
from pathlib import Path

import cv2
import numpy as np
import open_clip
import torch
from PIL import Image, ImageOps
from tqdm import tqdm

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
DB = "photos.db"
PROMPTS = [
    "a screenshot of a phone or computer screen",
    "a photo of a receipt, document or whiteboard",
    "a meme or image with text overlay",
    "a normal photo taken with a camera",
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


# ---------------------------------------------------------------- scan
def cmd_scan(args):
    root = Path(args.root)
    db = sqlite3.connect(DB)
    db.execute(
        """CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, sha TEXT, size INT, w INT, h INT,
        blur REAL, emb BLOB, p_shot REAL, p_doc REAL, p_meme REAL)"""
    )
    done = {r[0] for r in db.execute("SELECT path FROM files")}
    todo = [p for p in root.rglob("*")
            if p.suffix.lower() in EXT and str(p) not in done]
    print(f"{len(todo)} new images")

    dev = ("cuda" if torch.cuda.is_available()
           else "mps" if torch.backends.mps.is_available() else "cpu")
    model, _, pre = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="laion2b_s34b_b79k")
    model = model.to(dev).eval()
    tok = open_clip.get_tokenizer("ViT-B-32")
    with torch.no_grad():
        T = model.encode_text(tok(PROMPTS).to(dev))
        T /= T.norm(dim=-1, keepdim=True)

    B = 32
    for i in tqdm(range(0, len(todo), B)):
        rows, tensors = [], []
        for p in todo[i:i + B]:
            try:
                sha = sha256(p)
                im = load(p)
                w, h = im.size
                small = im.copy()
                small.thumbnail((1024, 1024))
                gray = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2GRAY)
                blur = cv2.Laplacian(gray, cv2.CV_64F).var()
                tensors.append(pre(im))
                rows.append((str(p), sha, p.stat().st_size, w, h, float(blur)))
            except Exception as e:
                print("skip", p, e)
        if not rows:
            continue
        with torch.no_grad():
            E = model.encode_image(torch.stack(tensors).to(dev))
            E /= E.norm(dim=-1, keepdim=True)
            P = (100 * E @ T.T).softmax(-1).cpu().numpy()
        E = E.cpu().numpy().astype("float32")
        db.executemany(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
            [r + (E[k].tobytes(), *map(float, P[k][:3]))
             for k, r in enumerate(rows)],
        )
        db.commit()


# -------------------------------------------------------------- report
def cmd_report(args):
    db = sqlite3.connect(DB)
    rows = db.execute(
        "SELECT path,sha,size,w,h,blur,emb,p_shot,p_doc,p_meme FROM files"
    ).fetchall()

    def score(r):  # higher = better keeper
        return (np.log(max(r[3] * r[4], 1)) + 0.5 * np.log1p(r[5])
                + r[2] * 1e-12)

    flags = {}  # path -> (reason, kept_path)

    # 1) exact duplicates
    by_hash = {}
    for i, r in enumerate(rows):
        by_hash.setdefault(r[1], []).append(i)
    for idx in by_hash.values():
        if len(idx) > 1:
            best = max(idx, key=lambda i: score(rows[i]))
            for i in idx:
                if i != best:
                    flags[rows[i][0]] = ("exact_dup", rows[best][0])

    # 2) near duplicates (only among files not already flagged)
    keep = [i for i in range(len(rows)) if rows[i][0] not in flags]
    E = np.stack([np.frombuffer(rows[i][6], dtype="float32") for i in keep])
    m = len(keep)
    parent = list(range(m))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s in tqdm(range(0, m, 1024), desc="clustering"):
        S = E[s:s + 1024] @ E.T
        for a, b in np.argwhere(S > args.threshold):
            i = s + a
            if b > i:
                parent[find(i)] = find(b)
    groups = {}
    for i in range(m):
        groups.setdefault(find(i), []).append(keep[i])
    for idx in groups.values():
        if len(idx) > 1:
            best = max(idx, key=lambda i: score(rows[i]))
            for i in idx:
                if i != best:
                    flags[rows[i][0]] = ("near_dup", rows[best][0])

    # 3) junk, tiny, blurry
    for r in rows:
        p = r[0]
        if p in flags:
            continue
        if r[3] * r[4] < 200 * 200:
            flags[p] = ("tiny", "")
        elif max(r[7:10]) > args.junk:
            flags[p] = ("junk", "")
        elif r[5] < args.blur:
            flags[p] = ("blurry", "")

    # outputs
    with open("flags.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "reason", "kept_instead"])
        for p, (reason, kept) in sorted(flags.items(), key=lambda x: x[1][0]):
            w.writerow([p, reason, kept])

    def img(p):
        return f'<img loading="lazy" src="{Path(p).as_uri()}" height="180">'

    parts = ["<meta charset=utf-8><body style='font-family:sans-serif'>"]
    parts.append(f"<h1>{len(flags)} flagged of {len(rows)}</h1>")
    for reason in ["exact_dup", "near_dup", "junk", "tiny", "blurry"]:
        items = [(p, k) for p, (r, k) in flags.items() if r == reason]
        parts.append(f"<h2>{reason} ({len(items)})</h2>")
        for p, kept in items[:500]:
            parts.append(
                "<div style='display:inline-block;margin:6px;"
                "border:1px solid #ccc;padding:4px'>"
                f"<div>DELETE {img(p)}</div>"
                + (f"<div>KEEP {img(kept)}</div>" if kept else "")
                + f"<small>{html.escape(Path(p).name)}</small></div>"
            )
    Path("report.html").write_text("".join(parts))
    print(f"{len(flags)} flagged. Open report.html. Edit flags.csv.")


# --------------------------------------------------------------- apply
def cmd_apply(args):
    root, q = Path(args.root), Path(args.quarantine)
    n = 0
    for r in csv.DictReader(open("flags.csv")):
        src = Path(r["path"])
        if not src.exists():
            continue
        dst = q / src.relative_to(root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        n += 1
    print(f"moved {n} files to {q}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)

    s = sub.add_parser("scan")
    s.add_argument("root")
    s.set_defaults(fn=cmd_scan)

    r = sub.add_parser("report")
    r.add_argument("--threshold", type=float, default=0.95)
    r.add_argument("--junk", type=float, default=0.80)
    r.add_argument("--blur", type=float, default=15.0)
    r.set_defaults(fn=cmd_report)

    a = sub.add_parser("apply")
    a.add_argument("root")
    a.add_argument("quarantine")
    a.set_defaults(fn=cmd_apply)

    args = ap.parse_args()
    args.fn(args)