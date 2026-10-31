"""Repeatable large-library benchmark on synthetic data in a temporary directory.

Never reads personal files. Run from any directory:

    python bench_library.py --files 50000 --scan-files 20000

Timings are relative: compare runs on the same machine, not across platforms.
"""
import argparse
import json
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import ArchiveCore, _normal, _path_key
from search_ux import explain_matches, parse_query

WORDS = ("合同 预算 项目 会议 纪要 发票 报告 方案 设计 研究 学习 课程 旅行 家庭 账单 需求 版本 客户 "
         "审批 交付 风险 计划 季度 产品 用户 数据 模型 流程 复盘 总结").split()
EXTS = [".pdf"] * 3 + [".docx"] * 3 + [".txt", ".md", ".xlsx", ".pptx", ".py"] + [".jpg"] * 5 + [".png"] * 2 + [".mp4", ".mp3", ".zip"]
TEXT_EXTS = {".pdf", ".docx", ".txt", ".md", ".xlsx", ".pptx", ".py"}


def timed(function, repeat=3):
    samples = []
    value = None
    for _ in range(repeat):
        started = time.perf_counter()
        value = function()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 1), value


def body(rng, target_chars):
    pieces, size = [], 0
    while size < target_chars:
        piece = "".join(rng.choices(WORDS, k=12)) + "。\n"
        pieces.append(piece)
        size += len(piece)
    return "".join(pieces)


def seed_catalogue(core, count, seed=7):
    """Insert rows directly, with the same shape a scan would write."""
    rng = random.Random(seed)
    paragraphs = [body(rng, int(min(200_000, rng.lognormvariate(8.6, 1.2)))) for _ in range(400)]
    now = time.time()
    source = core.add_source(core.data_dir.parent)
    rows, bodies = [], []
    for i in range(count):
        ext = rng.choice(EXTS)
        name = f"{rng.choice(WORDS)}{rng.choice(WORDS)}_{i}{ext}"
        folder = rng.choice(("工作", "学习", "生活", "项目甲", "项目乙"))
        relative = f"{folder}/{name}"
        text = rng.choice(paragraphs) if ext in TEXT_EXTS else ""
        if i % 997 == 0:
            text += "\n罕见线索星河预算"
        tags = ["归档"] if i % 50 == 0 else []
        path = f"/bench/{relative}"
        mtime = now - rng.random() * 5 * 365 * 86400
        rows.append((i + 1, source["id"], path, _path_key(path), relative, name, "File", ext,
                     rng.randint(1, 10 ** 7), mtime, int(mtime * 1e9),
                     json.dumps(tags, ensure_ascii=False), text[:170], int(i % 200 == 0), 0, "", "",
                     "ok" if text else "metadata", "", _normal(name), _normal(relative), _normal(" ".join(tags)), now))
        bodies.append((i + 1, text, core._search_value(name, relative, tags, text)))
        if len(rows) == 2000 or i == count - 1:
            with core._connect() as db:
                db.executemany(
                    "INSERT INTO files(id,source_id,path,path_key,relative_path,name,kind,ext,size,mtime,mtime_ns,tags,summary,favorite,missing,archive_path,content_hash,extraction_status,extraction_error,name_norm,path_norm,tags_norm,indexed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                db.executemany("INSERT INTO file_bodies(file_id,text,search_text) VALUES(?,?,?)", bodies)
                for file_id, _, search_text in bodies:
                    core._write_search_index(db, file_id, search_text)
            rows, bodies = [], []


def bench_search(core):
    results = {"sqlite": sqlite3.sqlite_version}
    results["stats_ms"], _ = timed(core.stats)
    queries = {"empty": "", "common_2char": "合同", "two_terms": "合同 预算", "rare": "星河预算", "typed_old_pdf": "去年 PDF 合同"}
    for label, query in queries.items():
        parsed = parse_query(query)

        def page():
            result = core.search_page(query=parsed["text"], exts=parsed["exts"] or None, after=parsed["after"], before=parsed["before"], limit=120)
            bodies = core.bodies(row["id"] for row in result["rows"]) if result["tokens"] else {}
            explain_matches(result["rows"], bodies, parsed["text"])
            return result

        results[label + "_ms"], result = timed(page)
        results[label + "_hits"] = result["total"]
    tracemalloc.start()
    core.search_page(query="合同", limit=120)
    results["peak_python_mb"] = round(tracemalloc.get_traced_memory()[1] / 1048576, 1)
    tracemalloc.stop()
    return results


def bench_scan(base, count):
    source = base / "scan-source"
    rng = random.Random(11)
    for i in range(count):
        folder = source / f"d{i % 200:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{rng.choice(WORDS)}_{i}.txt").write_text(body(rng, 2000), encoding="utf-8")
    core = ArchiveCore(base / "scan-data")
    core.add_source(source)
    started = time.perf_counter()
    first = core.scan_all()
    first_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    second = core.scan_all()
    idle_ms = (time.perf_counter() - started) * 1000
    return {"files": count, "first_scan_s": round(first_ms / 1000, 2), "indexed": first["indexed"],
            "idle_rescan_s": round(idle_ms / 1000, 2), "idle_skipped": second["skipped"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=50000)
    parser.add_argument("--scan-files", type=int, default=20000)
    args = parser.parse_args()
    report = {}
    with tempfile.TemporaryDirectory(prefix="roostfile-bench-") as temp:
        base = Path(temp)
        core = ArchiveCore(base / "catalogue" / "data")
        started = time.perf_counter()
        seed_catalogue(core, args.files)
        report["seed_s"] = round(time.perf_counter() - started, 1)
        report["db_mb"] = round(core.db_path.stat().st_size / 1048576, 1)
        report["search"] = bench_search(core)
        if args.scan_files:
            report["scan"] = bench_scan(base, args.scan_files)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
