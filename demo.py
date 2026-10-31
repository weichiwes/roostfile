"""Create clearly identified, synthetic samples. Never reads personal files."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from core import ArchiveCore

SAMPLES = {
    "en": {
        "Work/Harbor_product_proposal.md": "# Harbor product proposal\n\nGive every file a place, and make finding it easy.\n\n## The problem\nFiles end up scattered across folders, and names are hard to remember. People want to find a document from one phrase they recall.\n\n## Direction\nA quiet personal archive. It indexes files, sorts them by topic and keeps browsing and search simple.\n\n## This week\nTest adding a folder, searching and previewing from start to finish.\n\nThis file is sample data.",
        "Study/Human_computer_interaction_notes.md": "# Making search feel natural\n\nReading notes on human-computer interaction.\nRecognition beats recall: show something people can identify instead of asking for an exact name.\nInformation scent: summaries, dates and file types help people decide what to open.\nProgressive disclosure: browse first, then show the text and source after selection.\n\nKeywords: cognitive load, information architecture, interaction design.\nThis file is sample data.",
        "Personal/Home_equipment_list.csv": "Device,Location,Use\nRouter,Study,Home network\nMonitor,Desk,Writing and reading\nExternal drive,Bookshelf,Backups\nLamp,Bedroom,Night reading\nNote,Sample data,No real personal details\n",
        "Creative/Autumn_short_film_storyboard.txt": "Letters in autumn: storyboard draft\n\nShot 01: afternoon light falls on an old desk and a page turns.\nShot 02: a finger moves across notes and stops at a sentence from last autumn.\nShot 03: an album opens to a forgotten photo of the forest.\n\nColour: moss green, warm grey, soft amber.\nSound: room tone, paper, a very quiet piano.\nThis file is sample data.",
        "Code/sort_experiment.py": '"""Sample data: a small file sorting experiment."""\nfrom pathlib import Path\n\ndef classify(path):\n    return Path(path).suffix.lower()\n\n# Sort by extension first, then read the text\n',
        "Work/October_project_review.txt": "October project review\n\nThis month we explored interactions for the archive and built a search prototype.\nKeywords now find text inside files and keep each file in its original location.\nNext we will look at local semantic models and text recognition for scans.\nThose models need testing for size, speed and quality on ordinary computers.\nThis file is sample data.",
        "Study/Photography_and_light_index.md": "# Photography and light\n\nSide light makes surface detail easier to see.\nThis note demonstrates text search: type 'side light' to find it.\n\nThe collection covers lighting diagrams, colour references and footage.\nThis file is sample data.",
        "Saved/Cabin_in_the_forest_ideas.md": "# Cabin in the forest\n\nMake a digital space feel like a room you can stay in.\nA simple entrance, clear paths, soft light. Every file has its own place.\n\nKeywords: architecture, private archive, quiet, beauty, forest.\nThis file is sample data.",
        "Other/Inbox_to_sort.txt": "A temporary collection of sample notes.\nTry adding tags to this file.\nThey stay with it the next time the archive updates.\n",
    },
    "zh": {
        "工作/栖光计划_产品提案.md": "# 栖光计划 · 产品提案\n\n让资料有归处，让寻找更轻松。\n\n## 用户问题\n电脑里的资料散落在不同文件夹，文件名经常记不住。希望通过正文中的一点线索，就能找回想要的文件。\n\n## 产品方向\n安静的个人档案馆。自动建立档案、按主题归类，提供清晰的浏览与搜索。\n\n## 本周计划\n验证资料接入、搜索与预览的完整流程。\n\n此文件为演示资料。",
        "学习/人机交互_阅读笔记.md": "# 让寻找变得自然\n\n一本关于人机交互的阅读笔记。\n识别比回忆更容易：与其要求用户记住准确名称，不如提供可辨认的预览。\n信息气味：摘要、日期、类别共同帮助用户判断是否值得打开。\n渐进呈现：先浏览，选中后再显示正文和来源。\n\n关键词：认知负荷、信息架构、UX、交互设计。\n此文件为演示资料。",
        "生活/家庭设备清单.csv": "设备,位置,用途\n路由器,书房,本地网络\n显示器,工作桌,创作与阅读\n移动硬盘,书柜,离线备份\n台灯,卧室,夜间阅读\n备注,演示资料,无真实个人信息\n",
        "创作/秋日短片_分镜草案.txt": "秋日来信 · 分镜草案\n\n镜头01：午后的光落在一张旧书桌上，书页微微翻动。\n镜头02：手指划过笔记，停在去年秋天的一个句子。\n镜头03：打开相册，寻找那张被遗忘的森林照片。\n\n色彩：苔绿、暖灰、柔和的琥珀。\n声音：环境底噪、纸张摩擦、很轻的钢琴。\n此文件为演示资料。",
        "代码/归档小实验.py": '"""演示资料：一个简单的文件分类实验。"""\nfrom pathlib import Path\n\ndef classify(path):\n    return Path(path).suffix.lower()\n\n# 按扩展名先分一轮，再看正文\n',
        "工作/十月项目回顾.txt": "十月项目回顾\n\n本月完成了档案库的交互探索与检索原型。\n当前可以通过关键词找到正文，并保留原文件的位置。\n接下来考虑本地语义模型和扫描文档的文字识别。\n本地模型需要评估体积、速度以及普通电脑上的效果。\n此文件为演示资料。",
        "学习/摄影与光线_资料索引.md": "# 摄影与光线\n\n侧光可以让表面的细节更容易被看见。\n这份笔记用于演示正文搜索：可以输入‘侧光’找到它。\n\n资料包括布光示意、色彩参考和影像素材。\n此文件为演示资料。",
        "收藏/森林里的小屋_灵感记录.md": "# 森林里的小屋\n\n把数字空间做得像可以停留的房间。\n入口简洁，路径清楚，光线柔和。每份资料有自己的位置。\n\n灵感关键词：建筑、私人档案馆、安静、美学、森林。\n此文件为演示资料。",
        "其他/收集箱_待整理.txt": "临时收集的演示资料。\n试着为这份文件加上标签。\n下一次更新档案时标签仍会保留。\n",
    },
}
IMAGE_NAME = {"en": "Design/Forest_light_colour_reference.png", "zh": "设计/林间光影_色彩参考.png"}
FAVORITE_TAGS = {
    "en": {"Harbor": ["In progress", "Product design"], "Cabin": ["Ideas", "Spaces"]},
    "zh": {"栖光": ["进行中", "产品设计"], "森林": ["灵感", "空间"]},
}


def demo_data_dir(base, language):
    return base / ".demo-data" / language


def _write_image(path):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(900, 560, QImage.Format_RGB32)
    image.fill(QColor("#DDDDC9"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)

    def shape(color, *points):
        painter.setBrush(QColor(color))
        painter.drawPolygon([QPointF(x, y) for x, y in points])

    painter.setBrush(QColor("#EAE6C7"))
    painter.drawEllipse(420, 25, 380, 380)
    shape("#75846B", (0, 340), (225, 150), (455, 430), (730, 230), (900, 380), (900, 560), (0, 560))
    shape("#455E50", (0, 460), (235, 350), (570, 510), (900, 360), (900, 560), (0, 560))
    for x in (120, 210, 690, 760):
        shape("#344C41", (x, 235), (x + 10, 235), (x + 10, 490), (x, 490))
        shape("#536D55", (x - 60, 300), (x + 5, 100), (x + 75, 300))
    painter.end()
    image.save(str(path))


def prepare_demo(base, language="en"):
    sample = base / "demo-files" / language
    for relative, content in SAMPLES[language].items():
        path = sample / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    _write_image(sample / IMAGE_NAME[language])
    core = ArchiveCore(demo_data_dir(base, language))
    if not core.sources():
        core.add_source(sample, mode="index")
    core.scan_all()
    for row in core.search(limit=100):
        for word, tags in FAVORITE_TAGS[language].items():
            if word in row["name"]:
                core.set_favorite(row["id"], True)
                core.set_tags(row["id"], tags)
