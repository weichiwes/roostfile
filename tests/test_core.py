import ast
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import ArchiveCore
import core as core_module


class ArchiveCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(__file__).resolve().parent / ".tmp-root"
        self.temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.base = Path(self.temp.name)
        self.source = self.base / "用户资料"
        self.source.mkdir()
        self.core = ArchiveCore(self.base / "数据")

    def tearDown(self):
        self.core.close()
        self.assertTrue(self.base.resolve().is_relative_to(self.temp_root.resolve()))
        self.temp.cleanup()

    def write(self, relative, text):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_chinese_fulltext_multi_keyword_and_persistence(self):
        self.write("会议纪要.txt", "星河项目 预算审批 合同归档")
        self.write("其他.md", "星河项目 不含另一关键词")
        added = self.core.add_source(self.source)
        progress = []
        result = self.core.scan_all(progress.append)
        self.assertEqual(result["indexed"], 2)
        rows = self.core.search("预算 合同")
        self.assertEqual([r["name"] for r in rows], ["会议纪要.txt"])
        self.assertIsInstance(rows[0]["tags"], list)
        self.assertFalse(rows[0]["missing"])
        self.assertEqual(progress[-1]["phase"], "complete")
        reopened = ArchiveCore(self.base / "数据")
        self.assertEqual(reopened.sources()[0]["id"], added["id"])
        self.assertEqual(len(reopened.search("预算")), 1)

    def test_incremental_changes_and_manual_labels_survive(self):
        path = self.write("研究笔记.md", "学习资料与离线检索")
        self.core.add_source(self.source)
        self.core.scan_all()
        row = self.core.search()[0]
        self.core.set_tags(row["id"], "我的项目,重要")
        self.core.set_favorite(row["id"], True)
        unchanged = self.core.scan_all()
        self.assertEqual(unchanged["skipped"], 1)
        path.write_text("修改后的正文和新关键词竹林", encoding="utf-8")
        changed = self.core.scan_all()
        self.assertEqual(changed["indexed"], 1)
        row = self.core.search("竹林")[0]
        self.assertEqual(row["tags"], ["我的项目", "重要"])
        self.assertEqual(len(self.core.search(view="favorites")), 1)
        self.assertEqual(len(self.core.search("我的项目")), 1)

    def test_unavailable_source_keeps_old_records_and_copy(self):
        path = self.write("生活账单.txt", "电费 123 元")
        self.core.add_source(self.source, "copy")
        self.core.scan_all()
        row = self.core.search()[0]
        copy = Path(row["archive_path"])
        self.assertEqual(copy.read_text(encoding="utf-8"), path.read_text(encoding="utf-8"))
        moved = self.base / "暂时断开的磁盘"
        self.source.rename(moved)
        self.core.scan_all()
        self.assertFalse(self.core.get_file(row["id"])["missing"])
        self.assertEqual(self.core.sources()[0]["status"], "unavailable")
        self.assertTrue(copy.exists())

    def test_copy_versions_collisions_and_remove_source_are_safe(self):
        first = self.write("项目甲/同名.txt", "第一份内容")
        second = self.write("项目乙/同名.txt", "不同内容")
        source = self.core.add_source(self.source, "copy")
        self.core.scan_all()
        records = self.core.search()
        copies = [r["archive_path"] for r in records]
        self.assertEqual(len(set(copies)), 2)
        first.write_text("全新的第一份内容", encoding="utf-8")
        self.core.scan_all()
        newer = self.core.search("全新的")[0]
        self.assertNotIn(newer["archive_path"], copies)
        for copy in copies:
            self.assertTrue(Path(copy).exists())
        self.core.remove_source(source["id"])
        self.assertEqual(self.core.stats()["total"], 0)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(Path(newer["archive_path"]).exists())

    def test_missing_and_cancelled_scan(self):
        first = self.write("甲.txt", "甲")
        self.write("乙.txt", "乙")
        self.core.add_source(self.source)
        self.core.scan_all()
        first.unlink()
        cancel = threading.Event()
        cancel.set()
        result = self.core.scan_all(cancel=cancel)
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.core.stats()["missing"], 0)
        self.core.scan_all()
        self.assertEqual(self.core.stats()["missing"], 1)

    def test_office_extraction_and_corrupt_file_continue(self):
        with zipfile.ZipFile(self.source / "论文.docx", "w") as archive:
            archive.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="urn:w"><w:p><w:r><w:t>量子研究 可检索正文</w:t></w:r></w:p></w:document>')
        (self.source / "损坏.docx").write_bytes(b"not a zip file")
        self.write("正常.txt", "继续扫描不受影响")
        self.core.add_source(self.source)
        result = self.core.scan_all()
        self.assertEqual(result["indexed"], 3)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(self.core.search("可检索正文")[0]["name"], "论文.docx")
        self.assertEqual(len(self.core.search("不受影响")), 1)

    def test_overlap_data_exclusion_and_literal_wildcards(self):
        self.core.add_source(self.source)
        child = self.source / "子目录"
        child.mkdir()
        with self.assertRaises(ValueError):
            self.core.add_source(child)
        with self.assertRaises(ValueError):
            self.core.add_source(self.base / "数据")
        self.write("百分比.txt", "预算100%_已确认")
        self.write("普通.txt", "预算1000元")
        self.core.scan_all()
        self.assertEqual(len(self.core.search("100%_")), 1)
        # Selecting a parent may include the catalogue directory; it must never self-index.
        other = ArchiveCore(self.source / "档案数据库")
        other.add_source(self.source)
        other.scan_all()
        self.assertEqual(other.stats()["total"], 2)

    def test_remote_paths_rejected_before_filesystem_read(self):
        for remote in (r"\\server\share\资料", "//server/share/docs", "https://example.com/docs"):
            with self.assertRaisesRegex(ValueError, "Network paths"):
                self.core.add_source(remote)

    def test_label_edits_during_extraction_are_not_overwritten(self):
        path = self.write("研究笔记.txt", "最初正文")
        self.core.add_source(self.source)
        self.core.scan_all()
        file_id = self.core.search()[0]["id"]
        path.write_text("正文已修改，需要重新提取", encoding="utf-8")
        extraction_started = threading.Event()
        continue_extraction = threading.Event()
        original_extract = core_module.extract_text

        def slow_extract(*args):
            extraction_started.set()
            self.assertTrue(continue_extraction.wait(4))
            return original_extract(*args)

        failures = []

        def run_scan():
            try:
                self.core.scan_all()
            except Exception as exc:
                failures.append(exc)

        with patch.object(core_module, "extract_text", side_effect=slow_extract):
            thread = threading.Thread(target=run_scan)
            thread.start()
            self.assertTrue(extraction_started.wait(4))
            try:
                self.core.set_tags(file_id, ["并发编辑标签"])
                self.core.set_favorite(file_id, True)
            finally:
                continue_extraction.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        final = self.core.get_file(file_id)
        self.assertEqual(final["tags"], ["并发编辑标签"])
        self.assertTrue(final["favorite"])
        self.assertEqual(len(self.core.search("并发编辑标签", view="favorites")), 1)

    def test_same_length_corrupt_vault_object_is_preserved_and_repaired(self):
        first = self.write("甲.txt", "GOOD")
        self.core.add_source(self.source, "copy")
        self.core.scan_all()
        original_record = self.core.search()[0]
        damaged = Path(original_record["archive_path"])
        damaged.write_text("BADD", encoding="utf-8")
        second = self.write("乙.txt", "GOOD")
        self.core.scan_all()
        records = self.core.search()
        self.assertEqual(len(records), 2)
        self.assertEqual(damaged.read_text(encoding="utf-8"), "BADD")
        for record in records:
            self.assertNotEqual(record["archive_path"], str(damaged))
            self.assertEqual(Path(record["archive_path"]).read_text(encoding="utf-8"), "GOOD")
        self.assertEqual(len({r["archive_path"] for r in records}), 1)
        # A later duplicate reuses the verified replacement, not the damaged canonical path.
        self.write("丙.txt", "GOOD")
        self.core.scan_all()
        self.assertEqual(len({r["archive_path"] for r in self.core.search()}), 1)
        self.assertEqual(first.read_text(encoding="utf-8"), "GOOD")
        self.assertEqual(second.read_text(encoding="utf-8"), "GOOD")

    def test_switch_index_to_copy_fills_missing_archive_without_file_changes(self):
        path = self.write("工作资料.txt", "原位置保留，稍后追加副本")
        source = self.core.add_source(self.source, "index")
        self.core.scan_all()
        original_record = self.core.search()[0]
        self.assertEqual(original_record["archive_path"], "")
        updated_source = self.core.set_source_mode(source["id"], "copy")
        self.assertEqual(updated_source["mode"], "copy")
        self.assertEqual(self.core.sources()[0]["mode"], "copy")
        result = self.core.scan_all()
        archived = self.core.get_file(original_record["id"])
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(archived["mtime_ns"], original_record["mtime_ns"])
        self.assertTrue(path.exists())
        self.assertEqual(Path(archived["archive_path"]).read_text(encoding="utf-8"), path.read_text(encoding="utf-8"))
        reopened = ArchiveCore(self.base / "数据")
        self.assertEqual(reopened.sources()[0]["mode"], "copy")

    def test_switch_copy_to_index_preserves_history_and_refuses_during_scan(self):
        path = self.write("工作资料.txt", "归档前的正文")
        source = self.core.add_source(self.source, "copy")
        self.core.scan_all()
        archived = self.core.search()[0]
        historical_copy = Path(archived["archive_path"])
        self.core._scan_lock.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "scan to finish"):
                self.core.set_source_mode(source["id"], "index")
            self.assertEqual(self.core.sources()[0]["mode"], "copy")
        finally:
            self.core._scan_lock.release()
        self.core.set_source_mode(source["id"], "index")
        self.assertEqual(self.core.get_file(archived["id"])["archive_path"], str(historical_copy))
        path.write_text("切换模式后更新的正文", encoding="utf-8")
        self.core.scan_all()
        updated = self.core.get_file(archived["id"])
        self.assertEqual(updated["archive_path"], "")
        self.assertEqual(updated["content_hash"], "")
        self.assertIn("切换模式后", updated["text"])
        self.assertEqual(historical_copy.read_text(encoding="utf-8"), "归档前的正文")
        self.assertEqual(len(list(self.core.vault_dir.rglob("*.txt"))), 1)

    def test_filters_apply_before_paging_beyond_ten_thousand_rows(self):
        self.core.add_source(self.source)
        self.core.scan_all()
        source_id = self.core.sources()[0]["id"]
        now = time.time()
        rows = [(source_id, f"/样例/{i}.txt", f"/样例/{i}.txt", f"{i}.txt", f"{i}.txt", "Plain text",
                 ".pdf" if i == 0 else ".txt", 10, now - (i * 60), 0, now) for i in range(10_050)]
        with self.core._connect() as db:
            db.executemany("INSERT INTO files(source_id,path,path_key,relative_path,name,kind,ext,size,mtime,mtime_ns,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            db.execute("UPDATE files SET ext='.pdf' WHERE name='10049.txt'")
        everything = self.core.search_page(limit=20)
        self.assertEqual(everything["total"], 10_050)
        self.assertEqual(len(everything["rows"]), 20)
        oldest_pdf = self.core.search_page(exts=[".pdf"], limit=20)
        self.assertEqual(oldest_pdf["total"], 2)
        self.assertEqual(oldest_pdf["rows"][1]["name"], "10049.txt")
        second = self.core.search_page(limit=20, offset=20)
        self.assertEqual(second["rows"][0]["name"], "20.txt")
        self.assertEqual(self.core.search_page(exts=[])["total"], 0)
        recent = self.core.search_page(after=now - 90, before=now - 30)
        self.assertEqual([r["name"] for r in recent["rows"]], ["1.txt"])

    def test_search_page_carries_cover_text_but_not_the_whole_body(self):
        body = "很长的正文" * 2000 + "末尾线索"
        self.write("长文.txt", body)
        self.core.add_source(self.source)
        self.core.scan_all()
        row = self.core.search_page("末尾线索")["rows"][0]
        self.assertLessEqual(len(row["text"]), core_module.COVER_TEXT_CHARS)
        self.assertNotIn("search_text", row)
        self.assertIn("末尾线索", self.core.bodies([row["id"]])[row["id"]])
        self.assertEqual(self.core.get_file(row["id"])["text"], body)

    def test_index_matches_chinese_latin_single_characters_and_punctuation(self):
        self.write("甲.txt", "二字词合同与 Contract2026 以及 100%_已确认")
        self.write("乙.txt", "完全不相关")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.assertTrue(self.core._fts)
        for query, expected in (("合同", 1), ("合", 1), ("同与", 1), ("二字词合同", 1), ("contract", 1), ("TRACT2", 1), ("100%_", 1), ("%_", 1),
                                ("同合", 0), ("不存在的词", 0), ("合同 不相关", 0), ("不 相关", 1)):
            with self.subTest(query=query):
                self.assertEqual(self.core.search_page(query)["total"], expected)
        pasted = "二字词合同与" * 80
        self.assertEqual(self.core.search_page(pasted)["total"], 0)

    def test_edits_and_removal_keep_the_search_index_in_step(self):
        path = self.write("纪要.txt", "第一版正文石榴")
        source = self.core.add_source(self.source)
        self.core.scan_all()
        row = self.core.search_page("石榴")["rows"][0]
        self.core.set_tags(row["id"], ["专属标签"])
        self.assertEqual(self.core.search_page("专属标签")["total"], 1)
        self.core.set_tags(row["id"], [])
        self.assertEqual(self.core.search_page("专属标签")["total"], 0)
        path.write_text("第二版正文樱桃", encoding="utf-8")
        self.core.scan_all()
        self.assertEqual(self.core.search_page("石榴")["total"], 0)
        self.assertEqual(self.core.search_page("樱桃")["total"], 1)
        self.core.remove_source(source["id"])
        with self.core._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM file_search").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM file_bodies").fetchone()[0], 0)

    def sheet(self, name="账目.xlsx"):
        path = self.source / name
        with zipfile.ZipFile(path, "w") as book:
            book.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>项目</t></si><si><r><t>金</t></r><r><t>额</t></r></si><si><t>物业费</t></si></sst>')
            book.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                          '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                          '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2"><v>1200</v></c></row>'
                          '<row r="3"><c r="B3" t="inlineStr"><is><t>备注</t></is></c></row>'
                          '</sheetData></worksheet>')
        return path

    def test_an_excel_sheet_keeps_its_rows_and_columns(self):
        self.assertEqual(core_module.read_table(self.sheet(), ".xlsx"), [["项目", "金额"], ["物业费", "", "1200"], ["", "备注"]])

    def test_excel_text_has_its_words_and_numbers_but_not_its_internal_references(self):
        path = self.sheet()
        text = core_module.extract_text(path, path.stat().st_size)[0]
        for word in ("项目", "物业费", "1200", "备注"):
            self.assertIn(word, text)
        self.assertNotRegex(text, r"(?<![\d])[012](?![\d])")

    def test_a_csv_is_read_by_its_own_separator_and_encoding(self):
        comma = self.write("a.csv", "项目,金额\n物业费,1200\n")
        self.assertEqual(core_module.read_table(comma, ".csv"), [["项目", "金额"], ["物业费", "1200"]])
        semicolon = self.source / "b.csv"
        semicolon.write_bytes("项目;金额\n物业费;1200\n".encode("gbk"))
        self.assertEqual(core_module.read_table(semicolon, ".csv")[1], ["物业费", "1200"])
        self.assertEqual(core_module.read_table(self.write("c.tsv", "甲\t乙\n"), ".tsv"), [["甲", "乙"]])

    def test_a_broken_sheet_is_simply_not_a_table(self):
        self.assertEqual(core_module.read_table(self.write("坏.xlsx", "不是表格"), ".xlsx"), [])
        self.assertEqual(core_module.read_table(self.source / "不存在.csv", ".csv"), [])

    def test_a_long_sheet_stops_at_the_preview_limit(self):
        rows = core_module.read_table(self.write("长.csv", "一,二\n" * 1000), ".csv")
        self.assertEqual(len(rows), core_module.MAX_PREVIEW_ROWS)

    def office(self, name, parts):
        path = self.source / name
        with zipfile.ZipFile(path, "w") as archive:
            for part, xml in parts.items():
                archive.writestr(part, xml)
        return path

    def test_a_word_document_keeps_its_paragraphs(self):
        path = self.office("报告.docx", {"word/document.xml": '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>第一</w:t></w:r><w:r><w:t>段</w:t></w:r></w:p><w:p><w:r><w:t>第二段</w:t></w:r></w:p></w:body></w:document>'})
        self.assertEqual(core_module.extract_text(path, path.stat().st_size)[0], "第一段\n第二段")

    def test_slides_come_in_slide_order(self):
        slide = '<p:sld xmlns:p="p" xmlns:a="a"><a:p><a:r><a:t>{}</a:t></a:r></a:p></p:sld>'
        path = self.office("演示.pptx", {f"ppt/slides/slide{number}.xml": slide.format(f"第{number}页") for number in (10, 2, 1)})
        self.assertEqual(core_module.extract_text(path, path.stat().st_size)[0], "第1页\n第2页\n第10页")

    def tagged(self, name):
        row = self.core.search_page(name)["rows"][0]
        self.core.set_tags(row["id"], ["重要线索"])
        self.core.set_favorite(row["id"], True)
        self.core.mark_used(row["id"])
        return row["id"]

    def test_a_renamed_file_keeps_its_tags_favorite_and_recent_use(self):
        path = self.write("旧名字.txt", "正文内容")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.tagged("旧名字")
        path.rename(path.with_name("新名字.txt"))
        result = self.core.scan_all()
        self.assertEqual(result["moved"], 1)
        rows = self.core.search()
        self.assertEqual([row["name"] for row in rows], ["新名字.txt"])
        self.assertEqual(rows[0]["tags"], ["重要线索"])
        self.assertTrue(rows[0]["favorite"])
        self.assertGreater(rows[0]["last_used"], 0)
        self.assertEqual(self.core.search_page("重要线索")["total"], 1)
        self.assertEqual(self.core.stats()["total"], 1)

    def test_a_file_moved_to_another_added_folder_keeps_its_tags(self):
        path = self.write("工作/合同.txt", "正文内容")
        other = self.base / "另一处"
        other.mkdir()
        self.core.add_source(self.source)
        self.core.add_source(other)
        self.core.scan_all()
        self.tagged("合同")
        path.rename(other / "合同.txt")
        self.assertEqual(self.core.scan_all()["moved"], 1)
        self.assertEqual(self.core.search_page("重要线索")["rows"][0]["relative_path"], "合同.txt")

    def test_a_copy_leaves_the_original_row_alone(self):
        path = self.write("原件.txt", "正文内容")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.tagged("原件")
        twin = path.with_name("副件.txt")
        twin.write_bytes(path.read_bytes())
        os.utime(twin, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
        self.assertEqual(self.core.scan_all()["moved"], 0)
        self.assertEqual(self.core.search_page("重要线索")["rows"][0]["name"], "原件.txt")

    def test_two_files_that_match_equally_are_not_guessed_at(self):
        first, second = self.write("甲.txt", "同样长度"), self.write("乙.txt", "同样长度")
        os.utime(second, ns=(first.stat().st_atime_ns, first.stat().st_mtime_ns))
        self.core.add_source(self.source)
        self.core.scan_all()
        first.rename(first.with_name("丙.txt"))
        second.rename(second.with_name("丁.txt"))
        self.assertEqual(self.core.scan_all()["moved"], 0)

    def changed(self, *paths):
        return {str(self.source): [os.path.join(*path.split("/")) for path in paths]}

    def test_a_partial_scan_reads_only_what_changed(self):
        self.write("工作/甲.txt", "一")
        self.write("学习/乙.txt", "二")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.write("工作/新.txt", "新内容")
        read = []
        real = core_module.extract_text
        with patch.object(core_module, "extract_text", side_effect=lambda path, size: read.append(path.name) or real(path, size)):
            result = self.core.scan_all(only=self.changed("工作/新.txt"))
        self.assertEqual(read, ["新.txt"])
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(self.core.search_page("新内容")["total"], 1)
        self.assertEqual(self.core.stats()["total"], 3)
        self.assertFalse(any(row["missing"] for row in self.core.search()))

    def test_a_partial_scan_notices_a_deleted_file(self):
        gone = self.write("工作/删.txt", "一")
        self.write("工作/留.txt", "二")
        self.write("学习/乙.txt", "三")
        self.core.add_source(self.source)
        self.core.scan_all()
        gone.unlink()
        self.core.scan_all(only=self.changed("工作/删.txt"))
        self.assertEqual({row["name"]: row["missing"] for row in self.core.search()}, {"删.txt": True, "留.txt": False, "乙.txt": False})

    def test_a_partial_scan_notices_a_deleted_folder(self):
        self.write("工作/合同/甲.txt", "一")
        self.write("工作/合同/深处/乙.txt", "二")
        self.write("学习/丙.txt", "三")
        self.core.add_source(self.source)
        self.core.scan_all()
        shutil.rmtree(self.source / "工作" / "合同")
        self.core.scan_all(only=self.changed("工作/合同"))
        self.assertEqual({row["name"]: row["missing"] for row in self.core.search()}, {"甲.txt": True, "乙.txt": True, "丙.txt": False})

    def test_a_folder_moved_in_whole_is_read_with_everything_inside(self):
        self.core.add_source(self.source)
        outside = self.base / "外面"
        (outside / "子目录").mkdir(parents=True)
        (outside / "子目录" / "深处.txt").write_text("内容", encoding="utf-8")
        outside.rename(self.source / "搬进来")
        self.core.scan_all(only=self.changed("搬进来"))
        self.assertEqual([row["name"] for row in self.core.search()], ["深处.txt"])

    def test_a_rename_seen_by_a_partial_scan_keeps_tags(self):
        path = self.write("工作/旧名.txt", "正文内容")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.tagged("旧名")
        path.rename(path.with_name("新名.txt"))
        result = self.core.scan_all(only=self.changed("工作/旧名.txt", "工作/新名.txt"))
        self.assertEqual(result["moved"], 1)
        self.assertEqual(self.core.search_page("重要线索")["rows"][0]["name"], "新名.txt")

    def test_a_partial_scan_leaves_other_added_folders_alone(self):
        other = self.base / "另一处"
        other.mkdir()
        (other / "新.txt").write_text("内容", encoding="utf-8")
        self.core.add_source(self.source)
        self.core.add_source(other)
        self.core.scan_all(only={str(self.source): []})
        self.assertEqual(self.core.stats()["total"], 0)

    def test_subfolders_are_listed_one_level_at_a_time(self):
        self.write("工作/合同/甲.txt", "一")
        self.write("工作/合同/深处/乙.txt", "二")
        self.write("工作/方案/丙.txt", "三")
        self.write("工作/丁.txt", "四")
        source = self.core.add_source(self.source)
        self.core.scan_all()
        inside = self.core.subfolders(source["id"], "工作")
        self.assertEqual([(row["name"], row["count"]) for row in inside], [("合同", 2), ("方案", 1)])
        self.assertEqual(inside[0]["folder"], os.path.join("工作", "合同"))
        self.assertEqual([row["name"] for row in self.core.subfolders(source["id"], "")], ["工作"])
        self.assertEqual(self.core.subfolders(source["id"], "工作/丁.txt".replace("/", os.sep)), [])

    def test_a_new_file_is_reported_as_arrived_but_a_renamed_one_is_not(self):
        path = self.write("工作/旧名.txt", "正文内容")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.write("工作/新来的.txt", "另一份")
        path.rename(path.with_name("新名.txt"))
        result = self.core.scan_all(only=self.changed("工作/旧名.txt", "工作/新名.txt", "工作/新来的.txt"))
        self.assertEqual(result["moved"], 1)
        self.assertEqual([self.core.get_file(file_id)["name"] for file_id in result["arrived"]], ["新来的.txt"])

    def test_a_scan_with_nothing_new_reports_no_arrivals(self):
        self.write("a.txt", "一")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.assertEqual(self.core.scan_all()["arrived"], [])

    def test_macos_network_volumes_are_recognised(self):
        listing = """/dev/disk3s1 on / (apfs, local, journaled)
//me@nas/share on /Volumes/share (smbfs, nodev, nosuid)
nas:/export on /Volumes/my files (nfs, nodev)
/dev/disk4s2 on /Volumes/USB (apfs, local)
"""
        self.assertEqual(core_module.network_mounts(listing), ["/Volumes/share", "/Volumes/my files"])

    def test_a_folder_on_a_macos_network_volume_is_recognised(self):
        with patch.object(core_module, "_mounts_checked", [time.monotonic(), ["/Volumes/share"]]):
            self.assertTrue(core_module._on_network_volume("/Volumes/share/资料"))
            self.assertFalse(core_module._on_network_volume("/Volumes/shared/资料"))

    def test_a_file_still_in_icloud_counts_as_a_placeholder(self):
        self.assertTrue(core_module._cloud_placeholder(SimpleNamespace(st_flags=0x40000000)))
        self.assertFalse(core_module._cloud_placeholder(SimpleNamespace(st_flags=0)))

    def test_the_stub_icloud_leaves_behind_is_not_a_file(self):
        self.write(".报告.pdf.icloud", "x")
        self.write("真文件.txt", "内容")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.assertEqual([row["name"] for row in self.core.search()], ["真文件.txt"])
        self.assertTrue(core_module.is_noise(".报告.pdf.icloud"))

    def test_a_collection_keeps_files_wherever_they_are(self):
        self.write("工作/合同.txt", "一")
        self.write("财务/发票.txt", "二")
        self.write("学习/笔记.txt", "三")
        self.core.add_source(self.source)
        self.core.scan_all()
        rows = {row["name"]: row["id"] for row in self.core.search()}
        tax = self.core.create_collection("报税")
        self.core.collect(tax, [rows["合同.txt"], rows["发票.txt"], rows["发票.txt"]])
        self.assertEqual(self.core.collections(), [{"id": tax, "name": "报税", "count": 2}])
        self.assertEqual(self.core.search_page(collection=tax)["total"], 2)
        self.assertEqual(self.core.search_page("发票", collection=tax)["total"], 1)
        self.assertEqual(self.core.search_page("笔记", collection=tax)["total"], 0)
        self.assertEqual(self.core.collections_of(rows["发票.txt"]), {tax})
        self.core.uncollect(tax, rows["发票.txt"])
        self.assertEqual(self.core.search_page(collection=tax)["total"], 1)

    def test_collections_need_a_name_of_their_own(self):
        first = self.core.create_collection("报税")
        for name in ("", "   ", "报税"):
            with self.assertRaises(ValueError):
                self.core.create_collection(name)
        other = self.core.create_collection("Taxes")
        with self.assertRaises(ValueError):
            self.core.create_collection("taxes")
        with self.assertRaises(ValueError):
            self.core.rename_collection(other, "报税")
        self.core.rename_collection(first, "2026 报税")
        self.assertEqual(self.core.collections()[0]["name"], "2026 报税")

    def test_deleting_a_collection_leaves_the_files(self):
        path = self.write("合同.txt", "一")
        self.core.add_source(self.source)
        self.core.scan_all()
        collection = self.core.create_collection("临时")
        self.core.collect(collection, [self.core.search()[0]["id"]])
        self.core.delete_collection(collection)
        self.assertEqual(self.core.collections(), [])
        self.assertEqual(self.core.stats()["total"], 1)
        self.assertTrue(path.exists())

    def test_a_collection_follows_a_renamed_file(self):
        path = self.write("旧名字.txt", "正文内容")
        self.core.add_source(self.source)
        self.core.scan_all()
        collection = self.core.create_collection("项目")
        self.core.collect(collection, [self.core.search()[0]["id"]])
        path.rename(path.with_name("新名字.txt"))
        self.assertEqual(self.core.scan_all()["moved"], 1)
        self.assertEqual([row["name"] for row in self.core.search_page(collection=collection)["rows"]], ["新名字.txt"])

    def test_a_collection_exports_to_a_plain_list(self):
        self.write("合同.txt", "一")
        self.core.add_source(self.source)
        self.core.scan_all()
        row = self.core.search()[0]
        self.core.set_tags(row["id"], ["重要", "2026"])
        collection = self.core.create_collection("项目")
        self.core.collect(collection, [row["id"]])
        target = self.base / "清单.csv"
        self.assertEqual(self.core.export_collection(collection, target), 1)
        lines = target.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(lines[0], "name,path,tags,modified")
        self.assertIn("合同.txt", lines[1])
        self.assertIn('"重要, 2026"', lines[1])

    def test_recently_read_words_are_available_for_a_first_suggestion(self):
        self.write("截图.png", "x")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.core.save_ocr(self.core.search()[0]["id"], "星河科技 报价单")
        self.assertEqual(self.core.ocr_texts(), ["星河科技 报价单"])

    def test_folders_list_where_files_really_are(self):
        self.write("工作/合同/a.txt", "一")
        self.write("工作/b.txt", "二")
        self.write("学习/c.txt", "三")
        self.write("根目录.txt", "四")
        self.core.add_source(self.source)
        self.core.scan_all()
        folders = self.core.folders()
        self.assertEqual([(row["name"], row["count"]) for row in folders], [("工作", 2), ("学习", 1)])
        self.assertEqual(self.core.search_page(folder="工作")["total"], 2)
        self.assertEqual(self.core.search_page(folder="工", limit=1)["total"], 0)

    def test_several_added_folders_are_listed_as_well(self):
        self.write("工作/a.txt", "一")
        other = self.base / "另一处"
        (other / "照片").mkdir(parents=True)
        (other / "照片" / "b.txt").write_text("二", encoding="utf-8")
        first = self.core.add_source(self.source)
        second = self.core.add_source(other)
        self.core.scan_all()
        names = [row["name"] for row in self.core.folders()]
        self.assertEqual(sorted(names), sorted([self.source.name, "另一处", "工作", "照片"]))
        self.assertEqual(self.core.search_page(source_id=second["id"])["total"], 1)
        self.assertEqual(self.core.search_page(source_id=first["id"], folder="照片")["total"], 0)

    def test_file_types_are_counted_from_extensions(self):
        self.write("a.txt", "一")
        self.write("b.txt", "二")
        self.write("c.png", "三")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.assertEqual(self.core.ext_counts(), {".txt": 2, ".png": 1})

    def test_recently_used_files_come_back_newest_first_and_survive_updates(self):
        path = self.write("先用.txt", "一")
        self.write("后用.txt", "二")
        self.write("没用过.txt", "三")
        self.core.add_source(self.source)
        self.core.scan_all()
        rows = {row["name"]: row["id"] for row in self.core.search()}
        with patch.object(core_module.time, "time", return_value=1000):
            self.core.mark_used(rows["先用.txt"])
        with patch.object(core_module.time, "time", return_value=2000):
            self.core.mark_used(rows["后用.txt"])
        used = self.core.search_page(view="used", sort="used")
        self.assertEqual([row["name"] for row in used["rows"]], ["后用.txt", "先用.txt"])
        self.assertEqual(self.core.stats()["used"], 2)
        path.write_text("改过了", encoding="utf-8")
        self.core.scan_all()
        self.assertEqual(self.core.get_file(rows["先用.txt"])["last_used"], 1000)

    def test_relevance_keeps_filename_tag_body_location_order(self):
        self.write("合同审批.txt", "审批说明")
        self.write("甲记录.txt", "无关内容")
        self.write("乙记录.txt", "这是合同的正文")
        self.write("合同目录/丙记录.txt", "无关内容")
        self.core.add_source(self.source)
        self.core.scan_all()
        tagged = next(r for r in self.core.search() if r["name"] == "甲记录.txt")
        self.core.set_tags(tagged["id"], ["合同"])
        names = [r["name"] for r in self.core.search_page("合同")["rows"]]
        self.assertEqual(names, ["合同审批.txt", "甲记录.txt", "乙记录.txt", "丙记录.txt"])
        by_name = [r["name"] for r in self.core.search_page("合同", sort="name", relevance=False)["rows"]]
        self.assertEqual(by_name, sorted(by_name, key=str.casefold))

    def test_unchanged_rescan_reads_no_bodies_and_writes_no_rows(self):
        for i in range(5):
            self.write(f"资料{i}.txt", "正文内容")
        self.core.add_source(self.source)
        self.core.scan_all()
        statements = []
        original = self.core._connect

        from contextlib import contextmanager

        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(self.core, "_connect", traced):
            result = self.core.scan_all()
        self.assertEqual(result["skipped"], 5)
        self.assertFalse([sql for sql in statements if "file_bodies" in sql or "INSERT INTO files" in sql])
        self.assertLess(sum(1 for sql in statements if sql.lstrip().upper().startswith("UPDATE FILES")), 3)

    def test_files_are_read_at_the_same_time(self):
        for number in range(core_module.SCAN_WORKERS):
            self.write(f"{number}.txt", "内容")
        self.core.add_source(self.source)
        together = threading.Barrier(core_module.SCAN_WORKERS, timeout=10)

        def extract(path, size):
            together.wait()
            return "内容", "ok", ""

        with patch.object(core_module, "extract_text", extract):
            result = self.core.scan_all()
        self.assertEqual((result["indexed"], result["errors"]), (core_module.SCAN_WORKERS, 0))

    def test_a_failure_in_one_file_leaves_the_rest_of_its_batch(self):
        for number in range(6):
            self.write(f"{number}.txt", "内容")
        self.core.add_source(self.source)

        def extract(path, size):
            if path.name == "2.txt":
                raise RuntimeError("damaged")
            return "内容", "ok", ""

        with patch.object(core_module, "extract_text", extract):
            result = self.core.scan_all()
        self.assertEqual((result["indexed"], result["errors"]), (5, 1))

    def test_only_changes_a_scan_would_act_on_count(self):
        for path in ("desktop.ini", "工作\\Thumbs.db", "~$合同.docx", "下载\\a.crdownload", "b.tmp", "项目\\node_modules\\x.js", ".git/index"):
            self.assertTrue(core_module.is_noise(path), path)
        for path in ("合同.docx", "工作\\预算.xlsx", "photos/a.jpg"):
            self.assertFalse(core_module.is_noise(path), path)

    def scanned(self, name="扫描.pdf"):
        """A file extraction found no words in, as a scan or a photo is."""
        path = self.write(name, "x")
        return path

    def indexed(self, **results):
        with patch.object(core_module, "extract_text", return_value=results.get("extracted", ("", "metadata", core_module.NO_TEXT_LAYER))):
            return self.core.scan_all()

    def test_only_photos_and_scans_wait_to_be_read(self):
        self.write("笔记.txt", "已有文字")
        self.write("截图.png", "x")
        self.scanned()
        self.core.add_source(self.source)
        with patch.object(core_module, "extract_text", side_effect=lambda path, size: (
                ("已有文字", "ok", "") if path.suffix == ".txt" else ("", "metadata", core_module.NO_TEXT_LAYER if path.suffix == ".pdf" else "Indexed name, path and file details only."))):
            self.core.scan_all()
        self.assertEqual(sorted(row["ext"] for row in self.core.ocr_waiting()), [".pdf", ".png"])
        self.assertEqual(self.core.ocr_remaining(), 2)

    def test_a_large_scan_still_waits_to_be_read(self):
        self.scanned("大扫描件.pdf")
        self.core.add_source(self.source)
        self.indexed(extracted=("", "too_large", core_module.TOO_LARGE))
        self.assertEqual(self.core.ocr_remaining(), 1)

    def test_a_cloud_placeholder_is_never_read(self):
        self.write("云端.png", "x")
        self.core.add_source(self.source)
        with patch.object(core_module, "_cloud_placeholder", return_value=True):
            self.core.scan_all()
        self.assertEqual(self.core.ocr_waiting(), [])

    def test_words_read_from_a_scan_search_like_any_other_text(self):
        self.scanned()
        self.core.add_source(self.source)
        self.indexed()
        row = self.core.ocr_waiting()[0]
        self.core.save_ocr(row["id"], "增值税发票 星河科技")
        self.assertEqual(self.core.ocr_remaining(), 0)
        found = self.core.search_page("星河科技")["rows"]
        self.assertEqual(found[0]["name"], "扫描.pdf")
        self.assertEqual(found[0]["extraction_status"], "ocr")
        self.assertEqual(found[0]["summary"], "增值税发票 星河科技")
        self.assertEqual(self.core.get_file(row["id"])["extraction_error"], "")

    def test_a_file_with_no_words_is_not_read_again(self):
        self.scanned()
        self.core.add_source(self.source)
        self.indexed()
        self.core.save_ocr(self.core.ocr_waiting()[0]["id"], "")
        self.assertEqual(self.core.ocr_waiting(), [])

    def test_words_survive_a_rescan_and_a_changed_file_is_read_again(self):
        path = self.scanned()
        self.core.add_source(self.source)
        self.indexed()
        self.core.save_ocr(self.core.ocr_waiting()[0]["id"], "星河科技")
        self.assertEqual(self.core.scan_all()["skipped"], 1)
        self.assertEqual(self.core.ocr_waiting(), [])
        self.assertEqual(self.core.search_page("星河科技")["total"], 1)
        path.write_text("changed content", encoding="utf-8")
        self.indexed()
        self.assertEqual(self.core.ocr_remaining(), 1)

    def test_saved_words_keep_the_users_tags_searchable(self):
        self.scanned()
        self.core.add_source(self.source)
        self.indexed()
        row = self.core.ocr_waiting()[0]
        self.core.set_tags(row["id"], ["年度归档"])
        self.core.save_ocr(row["id"], "星河科技")
        self.assertEqual(self.core.search_page("年度归档")["total"], 1)
        self.assertEqual(self.core.search_page("星河科技")["total"], 1)

    def test_names_of_one_document_share_a_family(self):
        family = {core_module.version_key("工作/合同/" + name, name) for name in (
            "合同终稿.docx", "合同终稿(1).docx", "合同终稿（2）.docx", "合同终稿-最新.docx", "合同终稿-最新-改.docx", "合同 - 副本.docx", "合同_v3.docx", "合同 v3.1.docx", "合同 final.docx")}
        self.assertEqual(family, {"工作/合同|合同.docx"})

    def test_look_alike_names_are_not_versions(self):
        self.assertNotEqual(core_module.version_key("a/方案.docx", "方案.docx"), core_module.version_key("a/方案更改.docx", "方案更改.docx"))
        self.assertNotEqual(core_module.version_key("a/合同.docx", "合同.docx"), core_module.version_key("b/合同.docx", "合同.docx"))
        self.assertNotEqual(core_module.version_key("a/合同.docx", "合同.docx"), core_module.version_key("a/合同.pdf", "合同.pdf"))
        for name in ("新建文本文档 (3).txt", "新建 Microsoft Word 文档.docx", "untitled (2).txt", "IMG_2010.jpg", "script_v2.py"):
            self.assertEqual(core_module.version_key("a/" + name, name), "", name)

    def test_a_family_knows_its_newest_file(self):
        old = self.write("报价.docx", "旧")
        new = self.write("报价-最新.docx", "新")
        other = self.write("备忘.txt", "无关")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        self.core.add_source(self.source)
        self.core.scan_all()
        family = self.core.search_page("报价", limit=5)["rows"][0]["version_key"]
        self.assertEqual([row["name"] for row in self.core.versions(family)], ["报价-最新.docx", "报价.docx"])
        total, newest = self.core.version_counts([family])[family]
        self.assertEqual((total, self.core.get_file(newest)["name"]), (2, "报价-最新.docx"))
        self.assertEqual(self.core.version_counts([""]), {})
        self.assertTrue(other.exists())

    def test_windows_system_files_stay_out_of_the_archive(self):
        self.write("desktop.ini", "[.ShellClassInfo]")
        self.write("Thumbs.db", "x")
        self.write("笔记.txt", "内容")
        self.core.add_source(self.source)
        self.assertEqual(self.core.scan_all()["indexed"], 1)
        self.assertEqual([row["name"] for row in self.core.search()], ["笔记.txt"])

    def test_a_file_held_open_during_a_scan_is_read_next_time(self):
        self.write("占用.txt", "季度预算")
        self.core.add_source(self.source)
        with patch.object(core_module, "extract_text", return_value=("", "unreadable", "Permission denied")):
            self.assertEqual(self.core.scan_all()["errors"], 1)
        self.assertEqual(self.core.search("预算"), [])
        self.assertEqual(self.core.scan_all()["errors"], 0)
        self.assertEqual([row["name"] for row in self.core.search("预算")], ["占用.txt"])

    def test_a_folder_too_deep_for_windows_does_not_stall_missing_files(self):
        path = self.write("将删除.txt", "内容")
        source = self.core.add_source(self.source)
        self.core.scan_all()
        row = self.core.search()[0]
        path.unlink()
        real_walk = os.walk

        def walk(root, onerror=None, **kwargs):
            onerror(OSError(2, "too long", "x" * 300))
            return real_walk(root, onerror=onerror, **kwargs)

        with patch.object(core_module.os, "walk", walk), patch.object(core_module, "_path_too_long", return_value=True):
            self.assertEqual(self.core.scan_all()["errors"], 1)
        self.assertTrue(self.core.get_file(row["id"])["missing"])
        stored = next(item for item in self.core.sources() if item["id"] == source["id"])
        self.assertEqual((stored["status"], stored["last_error"]), ("ready", core_module.TOO_DEEP))

    def test_only_windows_folder_errors_on_deep_paths_count_as_too_long(self):
        deep = SimpleNamespace(winerror=206, filename="x" * 300)
        self.assertTrue(core_module._path_too_long(deep))
        self.assertFalse(core_module._path_too_long(SimpleNamespace(winerror=206, filename="short")))
        self.assertFalse(core_module._path_too_long(SimpleNamespace(winerror=5, filename="x" * 300)))
        self.assertFalse(core_module._path_too_long(PermissionError(13, "denied", "x" * 300)))

    def test_links_are_refused_but_a_cloud_folder_is_not(self):
        self.assertTrue(core_module._redirects(SimpleNamespace(st_reparse_tag=0xA0000003)))
        self.assertTrue(core_module._redirects(SimpleNamespace(st_reparse_tag=0xA000000C)))
        self.assertFalse(core_module._redirects(SimpleNamespace(st_reparse_tag=0x9000601A)))
        self.assertFalse(core_module._redirects(SimpleNamespace()))

    def test_missing_flag_recovers_when_a_file_returns(self):
        path = self.write("暂离.txt", "内容")
        self.core.add_source(self.source)
        self.core.scan_all()
        row = self.core.search()[0]
        moved = self.base / "暂离.txt"
        path.rename(moved)
        self.core.scan_all()
        self.assertTrue(self.core.get_file(row["id"])["missing"])
        moved.rename(path)
        self.core.scan_all()
        self.assertFalse(self.core.get_file(row["id"])["missing"])

    def test_pdf_indexed_without_parser_is_read_again_once_available(self):
        self.write("报告.pdf", "not really a pdf")
        with patch.object(core_module, "extract_text", return_value=("", "metadata", core_module.NO_PDF_PARSER)):
            self.core.add_source(self.source)
            self.core.scan_all()
        record = self.core.search()[0]
        self.assertEqual(record["extraction_error"], core_module.NO_PDF_PARSER)
        with patch.object(core_module, "_pdf_parser_available", return_value=True), \
                patch.object(core_module, "extract_text", return_value=("补读出来的正文", "ok", "")):
            result = self.core.scan_all()
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(self.core.search_page("补读出来")["total"], 1)
        with patch.object(core_module, "_pdf_parser_available", return_value=True):
            self.assertEqual(self.core.scan_all()["skipped"], 1)

    def test_real_pdf_text_is_searchable(self):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest("pypdf is optional")
        stream = b"BT /F1 18 Tf 40 700 Td (Quarterly budget review) Tj ET"
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        body, offsets = b"%PDF-1.4\n", []
        for number, content in enumerate(objects, 1):
            offsets.append(len(body))
            body += b"%d 0 obj\n%s\nendobj\n" % (number, content)
        start = len(body)
        body += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        body += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % start
        (self.source / "report.pdf").write_bytes(body)
        self.core.add_source(self.source)
        self.core.scan_all()
        rows = self.core.search_page("budget review")["rows"]
        self.assertEqual([r["name"] for r in rows], ["report.pdf"])
        self.assertEqual(rows[0]["extraction_status"], "ok")

    def test_network_path_message_is_plain(self):
        with self.assertRaises(ValueError) as caught:
            self.core.add_source(r"\\\\server\\share")
        self.assertIn("Network paths", str(caught.exception))


    def test_removing_a_source_leaves_none_of_its_text_behind(self):
        marker, words = "ZQXJVKPRIVATE", "私密标记词语"
        (self.source / "secret.txt").write_text(f"{marker} {words}", encoding="utf-8")
        added = self.core.add_source(self.source)
        self.core.scan_all()
        def stored(value):
            return sum(path.read_bytes().count(value.encode()) for path in self.core.data_dir.iterdir() if path.is_file())
        self.assertGreater(stored(marker), 0)
        self.core.remove_source(added["id"])
        for value in (marker, marker.casefold(), words, words[:2]):
            self.assertEqual(stored(value), 0, value)

    def test_deleted_text_is_overwritten_on_every_connection(self):
        with self.core._connect() as db:
            self.assertEqual(db.execute("PRAGMA secure_delete").fetchone()[0], 1)

    def test_no_module_can_reach_a_network(self):
        banned = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "telnetlib", "xmlrpc", "webbrowser", "requests", "httpx", "aiohttp"}
        root = Path(core_module.__file__).parent
        for name in ("core", "search_ux", "mcp_server", "desktop_app", "assistant", "character", "background", "visual_widgets", "ocr", "pages", "demo", "i18n", "theme"):
            source = (root / f"{name}.py").read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                self.assertFalse({module.split(".")[0] for module in modules} & banned, name)
            for word in ("QNetworkAccessManager", "QTcpSocket", "QUdpSocket", "QWebEngine", "QWebSocket", "QDesktopServices"):
                self.assertNotIn(word, source, name)

    def test_the_package_holds_no_local_paths_or_library(self):
        import package_windows
        python, vendor, out = self.base / "python", self.base / "vendor", self.base / "out"
        python.mkdir(); (python / "pythonw.exe").write_text("x")
        (vendor / "PySide6").mkdir(parents=True); (vendor / "PySide6" / "a.pyd").write_text("x")
        (vendor / "bin").mkdir(); (vendor / "bin" / "tool.exe").write_text(r"D:\somewhere\python.exe")
        (self.base / "data").mkdir(exist_ok=True)
        archive = package_windows.build(python, vendor, out, "app")
        names = zipfile.ZipFile(archive).namelist()
        self.assertIn("app/.vendor/PySide6/a.pyd", names)
        self.assertIn("app/character.py", names)
        self.assertIn("app/assets/roo/layers.json", names)
        self.assertIn("app/assets/roo/front-hair.png", names)
        self.assertFalse([name for name in names if "/bin/" in name or "/data/" in name or name.endswith((".sqlite3", "ui-state.json"))])

    def names(self):
        return sorted(row["name"] for row in self.core.search_page(limit=50)["rows"])

    def hand_over(self, *paths):
        return self.core.scan_all(only=self.core.adopt(paths))

    def test_a_handed_over_file_brings_nothing_else_from_its_folder(self):
        (self.source / "收下.txt").write_text("甲", encoding="utf-8")
        (self.source / "旁边.txt").write_text("乙", encoding="utf-8")
        (self.source / "子目录").mkdir()
        (self.source / "子目录" / "收下.txt").write_text("丙", encoding="utf-8")
        self.hand_over(self.source / "收下.txt")
        self.assertEqual(self.names(), ["收下.txt"])
        self.core.scan_all()
        self.assertEqual(self.names(), ["收下.txt"])
        self.core.scan_all(only={str(self.source): ["子目录/收下.txt".replace("/", os.sep)]})
        self.assertEqual(self.names(), ["收下.txt"])
        self.assertFalse(self.core.search_page(limit=1)["rows"][0]["missing"])

    def test_handing_over_a_second_file_adds_to_the_first(self):
        for name in ("一.txt", "二.txt", "三.txt"):
            (self.source / name).write_text(name, encoding="utf-8")
        self.hand_over(self.source / "一.txt")
        self.hand_over(self.source / "二.txt")
        self.assertEqual(self.names(), ["一.txt", "二.txt"])
        self.assertEqual(len(self.core.sources()), 1)

    def test_a_handed_over_folder_becomes_a_source(self):
        (self.source / "甲.txt").write_text("甲", encoding="utf-8")
        self.hand_over(self.source)
        self.assertEqual(self.names(), ["甲.txt"])
        self.assertEqual(self.core.sources()[0]["picks"], "")
        self.assertEqual([row["name"] for row in self.core.files_at([self.source])], ["甲.txt"])

    def test_a_file_already_covered_by_a_source_is_not_added_twice(self):
        (self.source / "内层").mkdir()
        (self.source / "内层" / "甲.txt").write_text("甲", encoding="utf-8")
        self.core.add_source(self.source)
        self.hand_over(self.source / "内层" / "甲.txt")
        self.assertEqual(self.names(), ["甲.txt"])
        self.assertEqual(len(self.core.sources()), 1)

    def test_adding_the_folder_keeps_handed_over_files_in_their_collections(self):
        inner = self.source / "内层"
        inner.mkdir()
        (inner / "甲.txt").write_text("甲", encoding="utf-8")
        (self.source / "外层.txt").write_text("乙", encoding="utf-8")
        self.hand_over(inner / "甲.txt")
        row = self.core.files_at([inner / "甲.txt"])[0]
        collection = self.core.create_collection("收好")
        self.core.collect(collection, [row["id"]])
        self.core.add_source(self.source)
        self.core.scan_all()
        self.assertEqual(self.names(), ["外层.txt", "甲.txt"])
        self.assertEqual(len(self.core.sources()), 1)
        moved = self.core.get_file(row["id"])
        self.assertEqual(moved["relative_path"], str(Path("内层") / "甲.txt"))
        self.assertEqual([found["id"] for found in self.core.search_page(collection=collection)["rows"]], [row["id"]])

    def test_files_handed_over_never_block_adding_their_folder_as_a_source(self):
        (self.source / "甲.txt").write_text("甲", encoding="utf-8")
        (self.source / "乙.txt").write_text("乙", encoding="utf-8")
        self.hand_over(self.source / "甲.txt")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.assertEqual(self.names(), ["乙.txt", "甲.txt"])

    def test_one_bad_item_does_not_lose_the_rest(self):
        (self.source / "甲.txt").write_text("甲", encoding="utf-8")
        inside = self.core.data_dir / "内部.txt"
        inside.write_text("不该收", encoding="utf-8")
        self.hand_over(inside, self.source / "甲.txt")
        self.assertEqual(self.names(), ["甲.txt"])
        with self.assertRaisesRegex(ValueError, "data folder"):
            self.core.adopt([inside])


    def test_one_unstorable_file_does_not_stop_the_scan(self):
        (self.source / "好.txt").write_text("甲", encoding="utf-8")
        (self.source / "坏.txt").write_text("乙", encoding="utf-8")
        other = self.base / "第二个"
        other.mkdir()
        (other / "也好.txt").write_text("丙", encoding="utf-8")
        self.core.add_source(self.source)
        self.core.add_source(other)
        real = core_module.version_key
        def explode(relative_path, name):
            if name == "坏.txt":
                raise OverflowError("simulated")
            return real(relative_path, name)
        with patch.object(core_module, "version_key", explode):
            self.core.scan_all()
        self.assertEqual(self.names(), ["也好.txt", "好.txt"])
        self.assertEqual([row["status"] for row in self.core.sources()], ["ready", "ready"])

    def test_broken_characters_in_extracted_text_are_dropped(self):
        self.assertEqual(core_module._clean_text("A\ud83dBC"), "ABC")

    def link_or_skip(self, link, target):
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            self.skipTest("This account cannot create links.")

    def test_a_change_notice_never_leads_out_of_the_chosen_folder(self):
        outside = self.base / "没选的文件夹"
        outside.mkdir()
        (outside / "私密.txt").write_text("不该读到", encoding="utf-8")
        self.link_or_skip(self.source / "链接", outside)
        self.core.add_source(self.source)
        self.core.scan_all(only={str(self.source): ["链接"]})
        self.core.scan_all(only={str(self.source): [os.path.join("链接", "私密.txt")]})
        self.core.scan_all(only={str(self.source): [os.path.join(os.pardir, "没选的文件夹", "私密.txt")]})
        self.assertEqual(self.names(), [])

    def test_freeing_up_cloud_space_keeps_what_was_read(self):
        (self.source / "云端.txt").write_text("押金两个月", encoding="utf-8")
        (self.source / "扫描.png").write_bytes(b"not really a picture")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.core.save_ocr(self.core.search_page("扫描")["rows"][0]["id"], "识别到的词语")
        with patch.object(core_module, "_cloud_placeholder", return_value=True), patch.object(core_module, "extract_text", side_effect=AssertionError("would download it")):
            result = self.core.scan_all()
        self.assertEqual(result["indexed"], 0)
        self.assertEqual(self.core.search_page("押金")["total"], 1)
        self.assertEqual(self.core.search_page("识别到的词语")["total"], 1)

    def test_a_new_cloud_placeholder_is_never_downloaded(self):
        (self.source / "云端.txt").write_text("内容", encoding="utf-8")
        self.core.add_source(self.source)
        with patch.object(core_module, "_cloud_placeholder", return_value=True), patch.object(core_module, "extract_text", side_effect=AssertionError("would download it")):
            self.core.scan_all()
        self.assertEqual(self.core.search_page(limit=5)["rows"][0]["extraction_status"], "cloud_placeholder")

    def test_a_deleted_folder_is_missing_not_unreadable(self):
        folder = self.source / "工作"
        folder.mkdir()
        (folder / "甲.txt").write_text("甲", encoding="utf-8")
        self.core.add_source(self.source)
        self.core.scan_all()
        shutil.rmtree(folder)
        self.core.scan_all(only={str(self.source): [os.path.join("工作", "甲.txt"), "工作"]})
        self.assertTrue(self.core.search_page(limit=5)["rows"][0]["missing"])
        self.assertEqual(self.core.sources()[0]["status"], "ready")

    def test_labels_follow_a_move_seen_in_two_scans(self):
        for first, second in ((0, 1), (1, 0)):
            with self.subTest(first=first):
                old_folder, new_folder = self.source / f"旧{first}", self.source / f"新{first}"
                old_folder.mkdir()
                new_folder.mkdir()
                (old_folder / f"文件{first}.txt").write_text(f"内容 {first} 独一无二", encoding="utf-8")
                self.core.add_source(self.source)
                self.core.scan_all()
                row = self.core.files_at([old_folder])[0]
                self.core.set_tags(row["id"], ["重要"])
                collection = self.core.create_collection(f"集{first}")
                self.core.collect(collection, [row["id"]])
                (old_folder / f"文件{first}.txt").rename(new_folder / f"文件{first}.txt")
                steps = [str(old_folder.name + os.sep + f"文件{first}.txt"), str(new_folder.name + os.sep + f"文件{first}.txt")]
                for index in (first, second):
                    self.core.scan_all(only={str(self.source): [steps[index]]})
                moved = self.core.files_at([new_folder])[0]
                self.assertIn("重要", self.core.get_file(moved["id"])["tags"])
                self.assertEqual([found["id"] for found in self.core.search_page(collection=collection)["rows"]], [moved["id"]])
                self.assertEqual(self.core.files_at([old_folder]), [])

    def test_punctuation_in_a_short_term_counts(self):
        (self.source / "a.txt").write_text("learning c# today", encoding="utf-8")
        (self.source / "b.txt").write_text("cat dog", encoding="utf-8")
        (self.source / "c.txt").write_text("rise 50 percent", encoding="utf-8")
        (self.source / "d.txt").write_text("rise 5% today", encoding="utf-8")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.assertEqual([row["name"] for row in self.core.search_page("c#")["rows"]], ["a.txt"])
        self.assertEqual([row["name"] for row in self.core.search_page("5%")["rows"]], ["d.txt"])

    def test_files_taken_over_by_a_folder_are_found_by_that_folder(self):
        inner = self.source / "内层目录"
        inner.mkdir()
        (inner / "甲.txt").write_text("甲", encoding="utf-8")
        (inner / "报价.docx").write_text("x", encoding="utf-8")
        (inner / "报价-最新.docx").write_text("xy", encoding="utf-8")
        self.hand_over(inner / "报价.docx", inner / "甲.txt")
        self.core.add_source(self.source)
        self.core.scan_all()
        self.assertEqual(self.core.search_page("内层目录")["total"], 3)
        family = self.core.files_at([inner / "报价.docx"])[0]["version_key"]
        self.assertEqual(len(self.core.versions(family)), 2)

    def test_a_file_in_an_ignored_folder_is_refused_not_lost(self):
        ignored = self.source / "node_modules" / "pkg"
        ignored.mkdir(parents=True)
        (ignored / "readme.md").write_text("说明", encoding="utf-8")
        self.core.add_source(self.source)
        with self.assertRaisesRegex(ValueError, "never indexed"):
            self.core.adopt([ignored / "readme.md"])

    def test_versions_stay_inside_their_own_source(self):
        other = self.base / "第二个"
        other.mkdir()
        (self.source / "报价.docx").write_text("甲", encoding="utf-8")
        (other / "报价.docx").write_text("乙乙", encoding="utf-8")
        self.core.add_source(self.source)
        self.core.add_source(other)
        self.core.scan_all()
        rows = self.core.search_page(limit=5)["rows"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(self.core.versions(self.core.get_file(row["id"])["version_key"])), 1)

    def test_a_handed_over_file_renamed_in_case_only_stays(self):
        path = self.source / "Report.txt"
        path.write_text("甲", encoding="utf-8")
        with patch.object(core_module.os.path, "normcase", str.casefold), patch.object(core_module, "_path_key", lambda value: str(value).casefold()):
            self.hand_over(path)
            row = self.core.files_at([path])[0]
            path.rename(self.source / "report.txt")
            self.core.scan_all()
        self.assertFalse(self.core.get_file(row["id"])["missing"])

    def test_handing_over_a_whole_source_again_notices_what_went(self):
        (self.source / "甲.txt").write_text("甲", encoding="utf-8")
        (self.source / "乙.txt").write_text("乙", encoding="utf-8")
        self.hand_over(self.source)
        (self.source / "乙.txt").unlink()
        self.hand_over(self.source)
        self.assertEqual(self.core.search_page("乙")["rows"][0]["missing"], True)

    def test_saved_copies_follow_the_archive_folder(self):
        (self.source / "甲.txt").write_text("甲", encoding="utf-8")
        self.core.add_source(self.source, mode="copy")
        self.core.scan_all()
        self.core.close()
        moved = self.base / "搬走"
        shutil.move(str(self.core.data_dir), str(moved))
        self.core = ArchiveCore(moved)
        row = self.core.get_file(self.core.search_page(limit=1)["rows"][0]["id"])
        self.assertTrue(row["archive_path"].startswith(str(self.core.data_dir)))
        self.assertTrue(Path(row["archive_path"]).is_file())

    def test_a_scan_reports_files_going_and_coming_back(self):
        path = self.source / "甲.txt"
        path.write_text("甲", encoding="utf-8")
        self.core.add_source(self.source)
        self.core.scan_all()
        kept = path.read_bytes(), path.stat()
        path.unlink()
        self.assertEqual(self.core.scan_all()["state_changes"], 1)
        path.write_bytes(kept[0])
        os.utime(path, ns=(kept[1].st_atime_ns, kept[1].st_mtime_ns))
        self.assertEqual(self.core.scan_all()["state_changes"], 1)
        self.assertEqual(self.core.scan_all()["state_changes"], 0)

    def test_files_at_skips_what_it_cannot_look_up(self):
        (self.source / "甲.txt").write_text("甲", encoding="utf-8")
        self.hand_over(self.source)
        self.assertEqual(len(self.core.files_at([r"\\server\share\x.txt", self.source / "甲.txt"])), 1)


if __name__ == "__main__":
    unittest.main()
