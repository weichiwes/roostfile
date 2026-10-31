"""Keeps both languages complete without needing Qt."""
import ast
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import i18n
from i18n import ZH, condition_label, t

UI_SOURCES = ("desktop_app.py", "visual_widgets.py")
CJK = re.compile(r"[\u3400-\u9fff]")


def translated_strings(source):
    tree = ast.parse((ROOT / source).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "t" and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                yield argument.value


class TranslationTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_every_interface_string_has_a_translation(self):
        for source in UI_SOURCES:
            for text in translated_strings(source):
                with self.subTest(source=source, text=text):
                    self.assertTrue(text in ZH, text)

    def test_core_messages_have_translations(self):
        tree = ast.parse((ROOT / "core.py").read_text(encoding="utf-8"))
        docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
        messages = {node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                    and re.fullmatch(r"[A-Z][^\n{}]{12,}\.", node.value)}
        self.assertGreater(len(messages), 12)
        internal = {"The file changed while copying. It will be retried on the next scan."}
        missing = sorted(messages - set(ZH) - internal)
        self.assertEqual(missing, [])
        for name in ("Documents", "Images", "Spreadsheets", "Media", "Code", "Other"):
            self.assertIn(name, ZH)

    def test_placeholders_match(self):
        for english, chinese in ZH.items():
            with self.subTest(text=english):
                self.assertEqual(set(re.findall(r"{(\w+)}", english)), set(re.findall(r"{(\w+)}", chinese)))

    def test_interface_code_has_no_chinese_literals(self):
        for source in UI_SOURCES:
            for number, line in enumerate((ROOT / source).read_text(encoding="utf-8").splitlines(), 1):
                with self.subTest(source=source, line=number):
                    self.assertIsNone(CJK.search(line.replace('"，"', "")), line)

    def test_interface_copy_stays_free_of_promotional_claims(self):
        banned = ("完全离线", "资料只在本机", "无需账号", "fully offline", "no account", "stays on your computer")
        for text in [*ZH, *ZH.values()]:
            for phrase in banned:
                self.assertNotIn(phrase, text)

    def test_chinese_spacing_rules(self):
        for english, chinese in ZH.items():
            with self.subTest(text=chinese):
                self.assertIsNone(re.search(r"[\u4e00-\u9fff][A-Za-z0-9]|[A-Za-z0-9][\u4e00-\u9fff]", chinese))

    def test_default_and_switching(self):
        self.assertEqual(i18n.language(), "en")
        self.assertEqual(i18n.app_name(), "Roostfile")
        self.assertEqual(t("Settings"), "Settings")
        i18n.set_language("zh")
        self.assertEqual(i18n.app_name(), "栖档")
        self.assertEqual(t("Settings"), "设置")
        self.assertEqual(t("{count} files", count=3), "3 份")
        self.assertEqual(t("{count} file", count=1), "1 份")
        self.assertEqual(condition_label("Last 14 days"), "最近 14 天")
        i18n.set_language("fr")
        self.assertEqual(i18n.language(), "en")


if __name__ == "__main__":
    unittest.main()
