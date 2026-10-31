"""Small, transparent search conveniences for the local archive.

This module performs no I/O and uses no model. ``parse_query`` removes only
recognised, clearly delimited type/date conditions. Everything else remains a
keyword. Pass ``resolve_filters(parsed, ...)`` to ``core.search_page`` so the
conditions run in SQL, then ``explain_matches`` to add snippets to that page.
``filter_and_rank`` applies the same rules to rows already held in memory.

Date intervals use [after, before), in the computer's own time zone. Relative
N-day intervals look back N * 24 hours from ``now``. Calendar periods follow
local calendar boundaries. An injected datetime/timestamp makes the behaviour reproducible.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any


DOCUMENT_EXTS = frozenset({
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".markdown",
    ".rst", ".tex", ".ppt", ".pptx", ".odp", ".pages", ".key", ".epub",
    ".html", ".htm",
})
IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".avif", ".svg", ".ico",
})
TABLE_EXTS = frozenset({".xls", ".xlsx", ".csv", ".tsv", ".ods", ".numbers"})
CODE_EXTS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".scss", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".java", ".rs", ".go", ".rb", ".php", ".vue",
    ".svelte", ".swift", ".kt", ".sql", ".sh", ".ps1", ".bat", ".xml",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".ipynb",
    ".r", ".m",
})
MEDIA_EXTS = frozenset({
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mp3", ".wav",
    ".flac", ".m4a", ".aac", ".ogg", ".aiff", ".aif",
})
KIND_EXTS = {
    "Documents": DOCUMENT_EXTS, "Images": IMAGE_EXTS, "Spreadsheets": TABLE_EXTS,
    "Code": CODE_EXTS, "Media": MEDIA_EXTS,
}

_VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"})
_SLIDE_EXTS = frozenset({".ppt", ".pptx", ".odp", ".key"})
# Query words work in both languages, whatever language the interface uses.
_TYPE_ALIASES: dict[str, tuple[frozenset[str], str]] = {
    "文档": (DOCUMENT_EXTS, "Documents"),
    "图片": (IMAGE_EXTS, "Images"),
    "照片": (IMAGE_EXTS, "Images"),
    "图像": (IMAGE_EXTS, "Images"),
    "表格": (TABLE_EXTS, "Spreadsheets"),
    "代码": (CODE_EXTS, "Code"),
    "影音": (MEDIA_EXTS, "Media"),
    "视频": (_VIDEO_EXTS, "Video"),
    "音频": (MEDIA_EXTS - _VIDEO_EXTS, "Audio"),
    "幻灯片": (_SLIDE_EXTS, "Slides"),
    "演示文稿": (_SLIDE_EXTS, "Slides"),
    "documents": (DOCUMENT_EXTS, "Documents"),
    "images": (IMAGE_EXTS, "Images"),
    "photos": (IMAGE_EXTS, "Images"),
    "pictures": (IMAGE_EXTS, "Images"),
    "spreadsheets": (TABLE_EXTS, "Spreadsheets"),
    "videos": (_VIDEO_EXTS, "Video"),
    "slides": (_SLIDE_EXTS, "Slides"),
    "presentations": (_SLIDE_EXTS, "Slides"),
    "pdf": (frozenset({".pdf"}), "PDF"),
    "pdfs": (frozenset({".pdf"}), "PDF"),
    "excel": (frozenset({".xls", ".xlsx", ".csv", ".tsv", ".ods"}), "Spreadsheets"),
    "ppt": (_SLIDE_EXTS, "Slides"),
    "markdown": (frozenset({".md", ".markdown"}), "Markdown"),
}
# Ordinary English words need an explicit form (type:code, .go): "code review
# notes" and "key results" are keywords, not file filters.
_EXPLICIT_ONLY: dict[str, tuple[frozenset[str], str]] = {
    "document": (DOCUMENT_EXTS, "Documents"),
    "image": (IMAGE_EXTS, "Images"),
    "photo": (IMAGE_EXTS, "Images"),
    "spreadsheet": (TABLE_EXTS, "Spreadsheets"),
    "code": (CODE_EXTS, "Code"),
    "media": (MEDIA_EXTS, "Media"),
    "video": (_VIDEO_EXTS, "Video"),
    "audio": (MEDIA_EXTS - _VIDEO_EXTS, "Audio"),
    "word": (frozenset({".doc", ".docx", ".odt"}), "Word"),
}
# An explicit extension is narrower than a broad product name such as Word.
for _extension in DOCUMENT_EXTS | IMAGE_EXTS | TABLE_EXTS | CODE_EXTS | MEDIA_EXTS:
    _alias = _extension[1:]
    if _alias not in _TYPE_ALIASES and _alias not in _EXPLICIT_ONLY:
        _EXPLICIT_ONLY[_alias] = (frozenset({_extension}), _alias.upper())
_BARE_EXTENSIONS = ("docx", "doc", "xlsx", "xls", "pptx", "csv", "png", "jpg", "jpeg", "webp", "gif", "svg", "heic", "mp4", "mkv", "mp3", "wav", "flac", "epub")
for _alias in _BARE_EXTENSIONS:
    _TYPE_ALIASES[_alias] = _EXPLICIT_ONLY.pop(_alias)
_ALL_ALIASES = {**_TYPE_ALIASES, **_EXPLICIT_ONLY}

_ALIASES_PATTERN = "|".join(re.escape(x) for x in sorted(_ALL_ALIASES, key=len, reverse=True))
_END = r"(?=$|[\s,，;；:：])"
# The relation form recognises e.g. PDF格式的合同, but not PDF阅读笔记 or PDF文件名.
_TYPE_PATTERN = re.compile(
    rf"(?<![^\s,，;；:：])(?P<prefix>(?:格式|类型|ext|type)[:：]\s*)?(?P<dot>\*?\.)?"
    rf"(?P<type>{_ALIASES_PATTERN})"
    rf"(?:(?:格式|文件)?(?:中的|里的|的)|(?:格式|文件)?{_END})",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(
    r"(?<![^\s,，;；:：])(?P<date>"
    r"(?:最近|近)\s*(?:[1-9]\d{0,3}|一|七|三十)\s*天|"
    r"(?:last|past)\s+[1-9]\d{0,3}\s+days?|"
    r"最近一周|近一周|最近一个月|近一个月|"
    r"上个月|这个月|本月|上月|本周|这周|上周|"
    r"今年|本年|去年|昨天|今天|"
    r"last\s+week|this\s+week|last\s+month|this\s+month|last\s+year|this\s+year|yesterday|today"
    r")(?:(?:中的|里的|的)|(?=$|[\s,，;；:：]))",
    re.IGNORECASE,
)
_PERIOD_WORDS = {
    "今天": "Today", "today": "Today", "昨天": "Yesterday", "yesterday": "Yesterday",
    "本周": "This week", "这周": "This week", "thisweek": "This week",
    "上周": "Last week", "lastweek": "Last week",
    "本月": "This month", "这个月": "This month", "thismonth": "This month",
    "上月": "Last month", "上个月": "Last month", "lastmonth": "Last month",
    "今年": "This year", "本年": "This year", "thisyear": "This year",
    "去年": "Last year", "lastyear": "Last year",
}
# Words people use for the same document, in either language. Only tried when a
# search finds nothing, and the interface says so, so a clue never silently changes.
_RELATED = (
    ("合同", "合约", "协议", "contract", "agreement"),
    ("发票", "票据", "收据", "invoice", "receipt"),
    ("简历", "履历", "resume", "cv"),
    ("报价", "报价单", "quote", "quotation"),
    ("预算", "budget"),
    ("报告", "report"),
    ("方案", "提案", "proposal"),
    ("会议", "纪要", "meeting", "minutes"),
    ("说明书", "手册", "manual"),
    ("工资", "薪资", "工资条", "salary", "payroll"),
    ("账单", "bill"),
    ("报销", "reimbursement", "expense"),
    ("租房", "租赁", "lease", "rent"),
    ("保险", "保单", "insurance"),
    ("体检", "checkup"),
    ("护照", "passport"),
    ("身份证", "id"),
    ("证书", "certificate"),
    ("论文", "paper", "thesis"),
    ("作业", "homework", "assignment"),
    ("笔记", "notes"),
    ("成绩单", "transcript"),
    ("截图", "截屏", "screenshot"),
    ("密码", "password"),
    ("旅行", "旅游", "travel", "trip"),
)
_RELATED_TERMS = {term: group for group in _RELATED for term in group}
# Any subject can be a report: 体检报告 is about the checkup.
_BROAD_TOPICS = ("报告", "方案")
# Whole short phrases only, so a long sentence never yields half a word.
_EXAMPLE_WORD = re.compile(r"(?<![\u4e00-\u9fff])[\u4e00-\u9fff]{2,6}(?![\u4e00-\u9fff])|(?<![A-Za-z])[A-Za-z]{4,12}(?![A-Za-z])")
_QUOTED = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' '''.rstrip())


def file_group(ext: str) -> str:
    """Documents, Images and so on: certain, because it comes from the file type alone."""
    return next((group for group, exts in KIND_EXTS.items() if ext in exts), "Other")


def _normal(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _now(value: dt.datetime | dt.date | float | None = None) -> dt.datetime:
    if value is None:
        return dt.datetime.now().astimezone()
    if isinstance(value, dt.datetime):
        return value.astimezone() if value.tzinfo is None else value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()).astimezone()
    return dt.datetime.fromtimestamp(float(value)).astimezone()


def _calendar_start(current: dt.datetime) -> dt.datetime:
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


def _bounds(label: str, current: dt.datetime) -> tuple[float | None, float | None, str]:
    key = re.sub(r"\s+", "", label).casefold()
    day = _calendar_start(current)
    match = re.fullmatch(r"(?:最近|近|last|past)([1-9]\d{0,3}|一|七|三十)(?:天|days?)", key)
    if match:
        n = {"一": 1, "七": 7, "三十": 30}.get(match[1])
        n = int(match[1]) if n is None else n
        return (current - dt.timedelta(days=n)).timestamp(), current.timestamp() + 0.000001, f"Last {n} days"
    if key in {"最近一周", "近一周"}:
        return _bounds("Last 7 days", current)
    if key in {"最近一个月", "近一个月"}:
        return _bounds("Last 30 days", current)
    period = _PERIOD_WORDS.get(key, "")
    if period == "Today":
        return day.timestamp(), (day + dt.timedelta(days=1)).timestamp(), period
    if period == "Yesterday":
        return (day - dt.timedelta(days=1)).timestamp(), day.timestamp(), period
    if period in {"This week", "Last week"}:
        monday = day - dt.timedelta(days=day.weekday())
        if period == "Last week":
            return (monday - dt.timedelta(days=7)).timestamp(), monday.timestamp(), period
        return monday.timestamp(), (monday + dt.timedelta(days=7)).timestamp(), period
    month = day.replace(day=1)
    if period == "Last month":
        previous = (month - dt.timedelta(days=1)).replace(day=1)
        return previous.timestamp(), month.timestamp(), period
    if period == "This month":
        following = month.replace(year=month.year + 1, month=1) if month.month == 12 else month.replace(month=month.month + 1)
        return month.timestamp(), following.timestamp(), period
    if period in {"This year", "Last year"}:
        year = current.year - (period == "Last year")
        return day.replace(year=year, month=1, day=1).timestamp(), day.replace(year=year + 1, month=1, day=1).timestamp(), period
    return None, None, label


def _intersect_lower(first: float | None, second: float | None) -> float | None:
    return second if first is None else first if second is None else max(first, second)


def _intersect_upper(first: float | None, second: float | None) -> float | None:
    return second if first is None else first if second is None else min(first, second)


def parse_query(query: str, now: dt.datetime | dt.date | float | None = None) -> dict[str, Any]:
    """Return keywords and explicit local type/date conditions.

    Examples: ``last 7 days pdfs contract``, ``photos forest``, ``上个月的发票``.
    Quote a condition to search for it literally: ``"PDF"``. Type terms inside
    filenames/words are never removed: ``PDF阅读笔记`` stays a keyword.
    """
    raw = unicodedata.normalize("NFKC", str(query or ""))
    searchable = list(raw)
    for quoted in _QUOTED.finditer(raw):
        searchable[quoted.start():quoted.end()] = " " * (quoted.end() - quoted.start())
    working = "".join(searchable)
    spans: list[tuple[int, int, str]] = []
    after = before = None
    current = _now(now)
    for match in _DATE_PATTERN.finditer(working):
        lower, upper, label = _bounds(match["date"], current)
        after = _intersect_lower(after, lower)
        before = _intersect_upper(before, upper)
        spans.append((match.start(), match.end(), label))
        searchable[match.start():match.end()] = " " * (match.end() - match.start())
    extensions: set[str] = set()
    working = "".join(searchable)
    for match in _TYPE_PATTERN.finditer(working):
        alias = match["type"].casefold()
        # Case-insensitive matching also lets near letters through, such as the dotless ı.
        if alias not in _ALL_ALIASES or (alias not in _TYPE_ALIASES and not (match["prefix"] or match["dot"])):
            continue
        group, label = _ALL_ALIASES[alias]
        extensions.update(group)
        spans.append((match.start(), match.end(), label))
    keywords = list(raw)
    for start, end, _ in spans:
        keywords[start:end] = " " * (end - start)
    text = re.sub(r"[\s,，;；]+", " ", "".join(keywords)).strip(" :：")
    labels = list(dict.fromkeys(label for _, _, label in sorted(spans)))
    return {"text": text, "exts": sorted(extensions), "after": after, "before": before, "labels": labels}


def _tokens(text: str) -> list[str]:
    return list(dict.fromkeys(
        _normal(token.strip("\"'")) for token in re.split(r"\s+", text.strip()) if token.strip("\"'")
    ))[:20]


def related_queries(text: str) -> list[str]:
    """Other wordings to try for a search that found nothing, one word changed at a time."""
    tokens = _tokens(text)
    return [
        " ".join((*tokens[:index], other, *tokens[index + 1:]))
        for index, token in enumerate(tokens)
        for other in _RELATED_TERMS.get(token, ())
        if other != token
    ][:12]


def topic_of(text: str, english: bool = False) -> str | None:
    """The everyday subject this text mentions most, or None.

    Short English words such as "id" mean too many things to go on.
    """
    normal, best, most = _normal(text), None, 0
    for group in _RELATED:
        hits = sum(len(re.findall(rf"(?<![a-z]){term}s?(?![a-z])", normal)) if term.isascii() else normal.count(term)
                   for term in group if len(term) >= 4 or not term.isascii())
        if hits > most or (hits == most and hits and best[0] in _BROAD_TOPICS):
            best, most = group, hits
    if best is None:
        return None
    return next(word for word in best if word.isascii()).capitalize() if english else best[0]


def example_word(texts: Iterable[str]) -> str | None:
    """A short word found in as many of these as possible, to show a search that will work."""
    seen = Counter(word for text in texts for word in set(_EXAMPLE_WORD.findall(str(text))))
    return min(seen, key=lambda word: (-seen[word], -len(word), word), default=None)


def _tags(row: Mapping[str, Any]) -> str:
    tags = row.get("tags") or []
    return " ".join(str(x) for x in tags) if isinstance(tags, (list, tuple, set)) else str(tags)


def _snippet(body: str, terms: list[str], limit: int = 135) -> str:
    compact = re.sub(r"\s+", " ", str(body or "")).strip()
    if not compact:
        return ""
    normal = _normal(compact)
    hits = [normal.find(term) for term in terms if term and normal.find(term) >= 0]
    first = min(hits) if hits else 0
    start = max(0, first - 38)
    end = min(len(compact), start + limit)
    # Use remaining space when a match occurs near the end of a document.
    start = max(0, min(start, end - limit))
    return ("…" if start else "") + compact[start:end] + ("…" if end < len(compact) else "")


def resolve_filters(
    parsed: Mapping[str, Any],
    kind: str = "All",
    period: str = "Any time",
    now: dt.datetime | dt.date | float | None = None,
) -> dict[str, Any]:
    """Combine typed conditions with the filter controls, as SQL-ready values.

    ``exts`` is None for no type limit, and empty when the two conditions
    exclude each other, which correctly matches nothing.
    """
    exts: set[str] | None = set(parsed.get("exts") or []) or None
    selected = KIND_EXTS.get(kind)
    if selected is not None:
        exts = set(selected) if exts is None else exts & selected
    after, before = parsed.get("after"), parsed.get("before")
    if period != "Any time":
        lower, upper, _ = _bounds(period, _now(now))
        after, before = _intersect_lower(after, lower), _intersect_upper(before, upper)
    return {"exts": sorted(exts) if exts is not None else None, "after": after, "before": before}


def explain_matches(
    rows: Iterable[Mapping[str, Any]],
    bodies: Mapping[int, str],
    keywords: str,
) -> list[dict[str, Any]]:
    """Add snippet and match source to a filtered, ordered page, keeping its order."""
    page = [dict(row) for row in rows]
    full = [dict(row, text=bodies.get(row["id"], row.get("text", ""))) for row in page]
    explained = {row["id"]: row for row in filter_and_rank(full, {"text": keywords})}
    for row in page:
        found = explained.get(row["id"])
        if found:
            row.update(snippet=found["snippet"], match_source=found["match_source"], match_terms=found["match_terms"])
    return page


def highlight_spans(text: str, terms: Iterable[str]) -> list[tuple[str, bool]]:
    """Split display text into (part, is_hit), for drawing matched words."""
    wanted = [term for term in dict.fromkeys(terms) if term]
    if not wanted or not text:
        return [(text, False)] if text else []
    pattern = re.compile("|".join(re.escape(term) for term in sorted(wanted, key=len, reverse=True)), re.IGNORECASE)
    parts: list[tuple[str, bool]] = []
    position = 0
    for match in pattern.finditer(text):
        if match.start() > position:
            parts.append((text[position:match.start()], False))
        parts.append((match.group(), True))
        position = match.end()
    if position < len(text):
        parts.append((text[position:], False))
    return parts


def filter_and_rank(
    rows: Iterable[Mapping[str, Any]],
    query: str | Mapping[str, Any],
    kind: str = "All",
    period: str = "Any time",
    now: dt.datetime | dt.date | float | None = None,
) -> list[dict[str, Any]]:
    """Filter candidates and return independent dicts with match explanations.

    ``query`` may be the original string or ``parse_query``'s result. Filename
    matches rank before tags, then body text, then path. Multiple
    keywords use AND across all fields. With no keywords, input order is kept
    so the caller's time/name/size ordering remains intact.
    """
    parsed = parse_query(query, now=now) if isinstance(query, str) else dict(query)
    terms = _tokens(str(parsed.get("text", "")))
    exts = {_normal(x) if str(x).startswith(".") else "." + _normal(x) for x in parsed.get("exts", [])}
    selected_exts = KIND_EXTS.get(kind)
    after, before = parsed.get("after"), parsed.get("before")
    if period != "Any time":
        lower, upper, _ = _bounds(period, _now(now))
        after, before = _intersect_lower(after, lower), _intersect_upper(before, upper)
    results: list[tuple[tuple[int | float, ...], dict[str, Any]]] = []
    for index, source in enumerate(rows):
        row = dict(source)
        ext = _normal(row.get("ext", ""))
        ext = ext if ext.startswith(".") or not ext else "." + ext
        if (exts and ext not in exts) or (selected_exts is not None and ext not in selected_exts):
            continue
        try:
            modified = float(row.get("mtime") or 0)
        except (TypeError, ValueError):
            modified = 0.0
        if (after is not None and modified < after) or (before is not None and modified >= before):
            continue
        body = str(row.get("text") or "")
        fields = {
            "name": _normal(row.get("name", "")),
            "tags": _normal(_tags(row)),
            "body": _normal(body),
            "location": _normal(str(row.get("relative_path", "")) + " " + str(row.get("path", ""))),
            "info": _normal(str(row.get("kind", "")) + " " + str(row.get("summary", ""))),
        }
        if terms and not all(any(term in field for field in fields.values()) for term in terms):
            continue
        hits = {name: sum(term in field for term in terms) for name, field in fields.items()}
        match_source = next((name for name, count in hits.items() if count), "info")
        # Body snippets show the actual paragraph containing the clue, even if
        # the stronger hit was in the filename or one of the user's tags.
        row["snippet"] = _snippet(body or str(row.get("summary", "")), terms)
        row["match_source"] = match_source
        row["match_terms"] = [term for term in terms if any(term in value for value in fields.values())]
        score = (
            int(bool(terms) and hits["name"] == len(terms)),
            hits["name"], hits["tags"], hits["body"], hits["location"],
            modified, -index,
        )
        results.append((score, row))
    if terms:
        results.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in results]
