#!/usr/bin/env python3
"""
python inventory.py build /Volumes/YourSSD/Takeout
python inventory.py stats
"""
import os
import sqlite3
import sys
from pathlib import Path

DB = "inventory.db"
VIDEO_EXT = {".mp4", ".mov", ".avi", ".m4v", ".3gp", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}


def build(root):
    root = Path(root)
    db = sqlite3.connect(DB)
    db.execute("DROP TABLE IF EXISTS files")
    # pure capture, no classification here
    db.execute(
        """CREATE TABLE files(
        path TEXT, name TEXT, ext TEXT, size INT, dir TEXT, mtime REAL)"""
    )
    rows = []
    n_dirs = 0
    for dirpath, dirnames, filenames in os.walk(root):
        n_dirs += 1
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            rows.append((
                str(p), fn, p.suffix.lower(), st.st_size, dirpath, st.st_mtime,
            ))
            if len(rows) >= 5000:
                db.executemany(
                    "INSERT INTO files VALUES (?,?,?,?,?,?)", rows)
                db.commit()
                rows = []
    if rows:
        db.executemany("INSERT INTO files VALUES (?,?,?,?,?,?)", rows)
        db.commit()
    db.execute("CREATE INDEX idx_ext ON files(ext)")
    db.execute("CREATE INDEX idx_dir ON files(dir)")
    db.commit()
    print(f"indexed. {n_dirs} directories, {sum(1 for _ in db.execute('SELECT 1 FROM files'))} files.")


def gb(b):
    return f"{b / 1e9:.2f} GB"


def classify(ext):
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".json":
        return "json"
    return "other"


def stats():
    db = sqlite3.connect(DB)
    db.create_function("classify", 1, classify)

    total_files, total_size = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
    n_dirs = db.execute(
        "SELECT COUNT(DISTINCT dir) FROM files").fetchone()[0]

    print(f"Total files: {total_files}")
    print(f"Total dirs:  {n_dirs}")
    print(f"Total size:  {gb(total_size)}")

    print("\nBy category:")
    for cat, cnt, sz in db.execute(
        """SELECT classify(ext), COUNT(*), SUM(size) FROM files
           GROUP BY classify(ext) ORDER BY sz DESC"""
    ):
        print(f"  {cat:<8} {cnt:>7}  {gb(sz)}")

    print("\nBy extension (top 15):")
    for ext, cnt, sz in db.execute(
        """SELECT ext, COUNT(*), SUM(size) FROM files
           GROUP BY ext ORDER BY sz DESC LIMIT 15"""
    ):
        print(f"  {ext or '(none)':<10} {cnt:>7}  {gb(sz)}")

    print("\n'other' extensions in full (not video/image/json):")
    other_exts = [r[0] for r in db.execute(
        "SELECT DISTINCT ext FROM files WHERE ? = classify(ext)", ("other",))]
    for ext, cnt, sz in db.execute(
        f"""SELECT ext, COUNT(*), SUM(size) FROM files
            WHERE ext IN ({','.join('?' * len(other_exts))})
            GROUP BY ext ORDER BY sz DESC""", other_exts
    ) if other_exts else []:
        print(f"  {ext or '(none)':<10} {cnt:>7}  {gb(sz)}")

    print("\nTop 20 biggest videos:")
    ext_list = list(VIDEO_EXT)
    for path, size in db.execute(
        f"""SELECT path, size FROM files WHERE ext IN ({','.join('?' * len(ext_list))})
            ORDER BY size DESC LIMIT 20""", ext_list
    ):
        print(f"  {gb(size):>10}  {path}")

    print("\nTop 10 biggest folders:")
    for d, sz in db.execute(
        """SELECT dir, SUM(size) as s FROM files
           GROUP BY dir ORDER BY s DESC LIMIT 10"""
    ):
        print(f"  {gb(sz):>10}  {d}")


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2])
    elif sys.argv[1] == "stats":
        stats()