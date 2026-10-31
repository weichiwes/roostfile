import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from search_ux import _now, example_word, explain_matches, filter_and_rank, highlight_spans, parse_query, related_queries, resolve_filters, topic_of


SHANGHAI = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 30, 14, 30, tzinfo=SHANGHAI)


def row(file_id, name, text="", tags=None, ext=".pdf", modified=None, **extra):
    return dict(id=file_id, name=name, text=text, tags=tags or [], ext=ext,
                mtime=(modified or NOW).timestamp(), summary="", **extra)


class QueryParsingTests(unittest.TestCase):
    def test_relative_date_type_and_remaining_keyword(self):
        parsed = parse_query("最近7天 PDF 合同", now=NOW)
        self.assertEqual(parsed["text"], "合同")
        self.assertEqual(parsed["exts"], [".pdf"])
        self.assertEqual(parsed["labels"], ["Last 7 days", "PDF"])
        self.assertEqual(parsed["after"], (NOW - dt.timedelta(days=7)).timestamp())
        self.assertGreater(parsed["before"], NOW.timestamp())

    def test_image_alias_and_unknown_words(self):
        parsed = parse_query("图片 森林，旅行", now=NOW)
        self.assertEqual(parsed["text"], "森林 旅行")
        self.assertIn(".png", parsed["exts"])
        self.assertIn(".jpeg", parsed["exts"])
        self.assertEqual(parsed["labels"], ["Images"])

    def test_previous_calendar_month(self):
        parsed = parse_query("上个月 发票", now=NOW)
        self.assertEqual(parsed["text"], "发票")
        self.assertEqual(parsed["after"], dt.datetime(2026, 9, 1, tzinfo=SHANGHAI).timestamp())
        self.assertEqual(parsed["before"], dt.datetime(2026, 10, 1, tzinfo=SHANGHAI).timestamp())

    def test_calendar_year_and_year_boundary(self):
        january = dt.datetime(2026, 1, 5, tzinfo=SHANGHAI)
        previous = parse_query("上个月的发票", now=january)
        self.assertEqual(previous["text"], "发票")
        self.assertEqual(previous["after"], dt.datetime(2025, 12, 1, tzinfo=SHANGHAI).timestamp())
        self.assertEqual(previous["before"], dt.datetime(2026, 1, 1, tzinfo=SHANGHAI).timestamp())
        current = parse_query("今年 合同", now=NOW)
        self.assertEqual(current["after"], dt.datetime(2026, 1, 1, tzinfo=SHANGHAI).timestamp())

    def test_terms_embedded_in_filenames_are_not_removed(self):
        for query in ["PDF阅读笔记", "PDF文件名", "去年总结.pdf", "图片处理脚本.py", "今天学习笔记"]:
            with self.subTest(query=query):
                parsed = parse_query(query, now=NOW)
                self.assertEqual(parsed["text"], query)
                self.assertEqual(parsed["exts"], [])
                self.assertEqual(parsed["labels"], [])

    def test_explicit_type_qualifier_and_extension(self):
        for query in ["PDF格式的合同", "PDF文件中的合同", "格式:pdf 合同", "*.pdf 合同", ".pdf 合同", "ＰＤＦ 合同"]:
            with self.subTest(query=query):
                parsed = parse_query(query, now=NOW)
                self.assertEqual(parsed["text"], "合同")
                self.assertEqual(parsed["exts"], [".pdf"])

    def test_quoted_conditions_are_literal_keywords(self):
        parsed = parse_query('"PDF" "上个月"', now=NOW)
        self.assertEqual(parsed["text"], '"PDF" "上个月"')
        self.assertEqual(parsed["labels"], [])
        self.assertEqual(parsed["exts"], [])

    def test_multiple_types_are_union_and_conflicting_dates_stay_visible(self):
        parsed = parse_query("上个月 最近7天 PDF DOCX 合同", now=NOW)
        self.assertEqual(parsed["exts"], [".docx", ".pdf"])
        self.assertGreater(parsed["after"], parsed["before"])
        self.assertEqual(parsed["labels"], ["Last month", "Last 7 days", "PDF", "DOCX"])

    def test_english_conditions(self):
        parsed = parse_query("last 7 days pdfs contract", now=NOW)
        self.assertEqual((parsed["text"], parsed["exts"]), ("contract", [".pdf"]))
        self.assertEqual(parsed["labels"], ["Last 7 days", "PDF"])
        parsed = parse_query("Last Month invoice", now=NOW)
        self.assertEqual(parsed["text"], "invoice")
        self.assertEqual(parsed["after"], dt.datetime(2026, 9, 1, tzinfo=SHANGHAI).timestamp())
        self.assertEqual(parse_query("past 14 days", now=NOW)["labels"], ["Last 14 days"])
        self.assertIn(".png", parse_query("photos forest", now=NOW)["exts"])

    def test_ordinary_english_words_stay_keywords(self):
        for query in ["key results", "go to market", "code review notes", "word count", "image processing", "bat flight", "audio transcript", "media plan"]:
            with self.subTest(query=query):
                parsed = parse_query(query, now=NOW)
                self.assertEqual(parsed["text"], query)
                self.assertEqual(parsed["exts"], [])

    def test_explicit_forms_reach_every_type(self):
        self.assertEqual(parse_query("type:code", now=NOW)["labels"], ["Code"])
        self.assertEqual(parse_query(".go server", now=NOW)["exts"], [".go"])
        self.assertEqual(parse_query("ext:key review", now=NOW)["exts"], [".key"])

    def test_naive_datetime_uses_client_timezone(self):
        parsed = parse_query("今天", now=dt.datetime(2026, 10, 30, 14, 30))
        self.assertEqual(parsed["after"], dt.datetime(2026, 10, 30, tzinfo=SHANGHAI).timestamp())


class SearchResultTests(unittest.TestCase):
    def test_filename_then_tags_then_body_then_location(self):
        rows = [
            row(4, "记录.pdf", path="D:/合同/记录.pdf"),
            row(3, "记录.pdf", text="这是合同的正文"),
            row(2, "记录.pdf", tags=["合同"]),
            row(1, "合同审批.pdf", text="审批说明"),
        ]
        results = filter_and_rank(rows, "合同", now=NOW)
        self.assertEqual([x["id"] for x in results], [1, 2, 3, 4])
        self.assertEqual([x["match_source"] for x in results], ["name", "tags", "body", "location"])

    def test_multi_keyword_and_across_fields(self):
        rows = [row(1, "合同审批.pdf", text="预算已经确认"),
                row(2, "合同审批.pdf", text="仅含一个词"),
                row(3, "审批.pdf", tags=["合同", "预算"])]
        results = filter_and_rank(rows, "合同 预算", now=NOW)
        self.assertEqual([x["id"] for x in results], [1, 3])

    def test_type_period_filter_and_exclusive_upper_boundary(self):
        rows = [row(1, "合同.pdf"),
                row(2, "合同.docx", ext=".docx"),
                row(3, "合同.pdf", modified=NOW - dt.timedelta(days=10)),
                row(4, "合同.pdf", modified=dt.datetime(2026, 10, 1, tzinfo=SHANGHAI)),
                row(5, "合同.pdf", modified=dt.datetime(2026, 9, 30, 23, 59, tzinfo=SHANGHAI))]
        results = filter_and_rank(rows, "最近7天 PDF 合同", now=NOW)
        self.assertEqual([x["id"] for x in results], [1])
        results = filter_and_rank(rows, "上个月 合同", kind="Documents", now=NOW)
        self.assertEqual([x["id"] for x in results], [5])
        results = filter_and_rank(rows, "合同", period="Last 7 days", now=NOW)
        self.assertEqual({x["id"] for x in results}, {1, 2})

    def test_image_kind_intersects_explicit_pdf_filter(self):
        rows = [row(1, "森林.jpg", ext=".jpg"), row(2, "森林.pdf", ext=".pdf")]
        self.assertEqual([x["id"] for x in filter_and_rank(rows, "森林", kind="Images")], [1])
        self.assertEqual(filter_and_rank(rows, "PDF 森林", kind="Images"), [])

    def test_body_snippet_shows_actual_match_not_just_first_paragraph(self):
        body = "很长的序言。" * 60 + "唯一的线索在这里：预算确认。" + "后文。" * 60
        result = filter_and_rank([row(1, "记录.pdf", text=body)], "预算")[0]
        self.assertIn("预算确认", result["snippet"])
        self.assertTrue(result["snippet"].startswith("…"))
        self.assertLessEqual(len(result["snippet"]), 137)
        self.assertEqual(result["match_terms"], ["预算"])

    def test_input_rows_unchanged_and_no_query_preserves_order(self):
        rows = [row(2, "B.pdf"), row(1, "A.pdf")]
        results = filter_and_rank(rows, "")
        self.assertEqual([x["id"] for x in results], [2, 1])
        self.assertNotIn("snippet", rows[0])
        self.assertIsNot(results[0], rows[0])

    def test_parsed_query_and_literal_condition_search(self):
        parsed = parse_query('"PDF"', now=NOW)
        results = filter_and_rank([row(1, "PDF阅读笔记.md", ext=".md"), row(2, "合同.pdf")], parsed)
        self.assertEqual({x["id"] for x in results}, {1, 2})
        parsed = parse_query("上个月的发票", now=NOW)
        results = filter_and_rank([row(1, "发票.pdf", modified=dt.datetime(2026, 9, 12, tzinfo=SHANGHAI))], parsed)
        self.assertEqual([x["id"] for x in results], [1])


class PagedSearchHelperTests(unittest.TestCase):
    def test_the_example_word_is_the_one_found_in_the_most_files(self):
        self.assertEqual(example_word(["购买方 星河 发票", "发票 金额", "发票 备注"]), "发票")

    def test_the_example_word_is_never_half_a_sentence(self):
        self.assertEqual(example_word(["增值税电子普通发票，报价单"]), "报价单")
        self.assertEqual(example_word(["Invoice 12345", "a b"]), "Invoice")

    def test_no_words_means_no_example(self):
        self.assertIsNone(example_word([]))
        self.assertIsNone(example_word(["12 34 ?"]))

    def test_dates_follow_the_computers_own_time_zone(self):
        moment = dt.datetime(2026, 6, 1, 12, 0)
        self.assertEqual(_now(moment).utcoffset(), moment.astimezone().utcoffset())
        self.assertEqual(_now(moment).hour, 12)
        self.assertEqual(_now(NOW).utcoffset(), dt.timedelta(hours=8))
        self.assertIsNotNone(_now().tzinfo)

    def test_related_words_work_across_languages_and_keep_the_other_keywords(self):
        self.assertIn("合同", related_queries("contract"))
        self.assertIn("invoice", related_queries("发票"))
        self.assertEqual(related_queries("海边"), [])
        self.assertIn("王总 合同", related_queries("王总 合约"))

    def test_resolve_filters_intersects_types_and_dates(self):
        parsed = parse_query("最近7天 PDF 合同", now=NOW)
        self.assertEqual(resolve_filters(parsed, now=NOW)["exts"], [".pdf"])
        self.assertEqual(resolve_filters(parsed, kind="Documents", now=NOW)["exts"], [".pdf"])
        self.assertEqual(resolve_filters(parsed, kind="Images", now=NOW)["exts"], [])
        self.assertIsNone(resolve_filters(parse_query("合同", now=NOW), now=NOW)["exts"])
        narrowed = resolve_filters(parse_query("今年", now=NOW), period="Last 7 days", now=NOW)
        self.assertEqual(narrowed["after"], (NOW - dt.timedelta(days=7)).timestamp())

    def test_explain_matches_keeps_order_and_returns_excerpt_not_body(self):
        body = "序言。" * 900 + "唯一线索预算"
        rows = [row(2, "乙.pdf", text="序言。"), row(1, "预算表.pdf", text="")]
        results = explain_matches(rows, {2: body, 1: ""}, "预算")
        self.assertEqual([x["id"] for x in results], [2, 1])
        self.assertIn("唯一线索预算", results[0]["snippet"])
        self.assertEqual(results[0]["match_source"], "body")
        self.assertEqual(results[0]["text"], "序言。")
        self.assertEqual(results[1]["match_source"], "name")

    def test_highlight_spans(self):
        self.assertEqual(highlight_spans("合同 Budget 合同", ["合同", "budget"]),
                         [("合同", True), (" ", False), ("Budget", True), (" ", False), ("合同", True)])
        self.assertEqual(highlight_spans("无命中", ["合同"]), [("无命中", False)])
        self.assertEqual(highlight_spans("", ["合同"]), [])
        self.assertEqual(highlight_spans("文本", []), [("文本", False)])


    def test_a_file_is_about_the_subject_it_mentions_most(self):
        self.assertEqual(topic_of("2026 年房屋租赁合同"), "合同")
        self.assertEqual(topic_of("发票抬头 发票号码 合同编号"), "发票")
        self.assertEqual(topic_of("invoice_2026.pdf", english=True), "Invoice")
        self.assertEqual(topic_of("Signed-Contracts.docx", english=True), "Contract")
        self.assertEqual(topic_of("sales contract"), "合同")

    def test_a_look_alike_letter_in_a_filter_is_just_text(self):
        self.assertEqual(parse_query("type:ımage")["exts"], [])

    def test_a_specific_subject_beats_a_broad_one(self):
        self.assertEqual(topic_of("体检报告.txt"), "体检")
        self.assertEqual(topic_of("保险方案.pdf"), "保险")
        self.assertEqual(topic_of("insurance report", english=True), "Insurance")
        self.assertEqual(topic_of("销售报告"), "报告")

    def test_a_vague_file_has_no_subject(self):
        self.assertIsNone(topic_of(""))
        self.assertIsNone(topic_of("holiday.jpg"))
        self.assertIsNone(topic_of("my id and cv"))
        self.assertIsNone(topic_of("contractor update", english=True))


if __name__ == "__main__":
    unittest.main()
