# Design

The goal is to help one person find their own files from a small clue. Browsing can feel like handling a collection. Searching, filtering and opening stay direct.

## Principles

**The files are the interface.** Interface chrome stays neutral so covers and images carry the colour. Nothing decorative competes with them.

**Colour means something.** The moss accent marks only what the user selected or matched, plus the one main action on screen. If everything is highlighted, nothing is.

**One neutral, a few strengths.** Text is a single warm off-white at three strengths, not a set of unrelated greys. Depth comes from hairlines rather than shadows or heavy fills.

**Hierarchy comes from size and strength, not weight.** Headings are regular weight. Bold type is the first thing that makes a dark interface look like a template.

**Give the space to content.** No breadcrumbs that do not navigate, no page title that repeats the sidebar, and no boxes around boxes. Search sits at the top; everything below it serves the result.

**Show the shape of what is coming.** Empty slots appear as dashed outlines of the deck rather than a blank page.

**Motion serves browsing.** The deck moves between fan and row over about 290 ms, cards lift on hover and neighbours make room. Search, filters, preview and opening are never delayed by animation, and Reduce motion turns it off.

**No claims in the interface.** Do not add copy such as "fully offline", "your data stays on this device" or "no account needed". These are true technical constraints, and they belong in the README.

## Tokens

Defined in `theme.py`.

| Token | Value | Use |
| --- | --- | --- |
| `BG` | `#0B0C0A` | Page, sidebar, details |
| `RAISED` | `#171815` | Search box, hover |
| `RAISED_HOVER` | `#1F201C` | Pressed, selected navigation |
| `HAIRLINE` | `#2A2B27` | Borders and separators |
| `TEXT` | `#F0EDE5` | Names and titles |
| `TEXT_2` | `#A9A7A0` | Body and controls |
| `TEXT_3` | `#85837D` | Metadata, at least 4.5:1 on `BG` |
| `ACCENT` | `#C2E0C9` | Selection, matches, the main action |
| `WARNING` | `#D9B98A` | A file that is not where it was |

Type sizes are 12, 13, 14, 16 and 19 px. Fonts are the system's: Segoe UI with Microsoft YaHei on Windows, and San Francisco with PingFang on macOS. No remote fonts or images are loaded.

Gutters between tiles are 10 px. Controls use a 6 px radius, covers 9 px and the search box 10 px.

## Finding a file

The main window has two routes. A third, quick search, works from anywhere.

- **By appearance.** The deck shows recent files. Arrow keys move through it, and the selected card stays centred.
- **By clue.** Typing switches to the list, and clearing the search returns to the deck. Recognised conditions appear as chips, so the user can see how the query was read. Words the parser does not recognise stay as keywords; it never pretends to understand arbitrary language.
- **When nothing matches.** A short list of related words is tried, in both languages, and the interface says which word it used. An exact match is never replaced. If that fails too, the page says why and offers the most useful next step.

While the first scan runs, files appear as they are read, and searching already works. Selecting a file opens the preview beside the results. Filename matches rank first, then tags, then text, then location. A result always shows why it matched.

Quick search is a small bar rather than the main window, because the person is in the middle of something else. It covers little, so the app behind it stays visible as a drop target. It starts on recent files, keeps typing in the box, and when it closes the keyboard goes back to the window that was in use, so a paste lands there straight away. Clicking elsewhere closes it. The default shortcut, `Ctrl Alt K`, echoes `Ctrl K` in the main window and avoids `Ctrl Space`, which switches Chinese input, and `Alt Space`, which belongs to the window menu and many assistants.

Most of what people remember about a photo of a receipt is words, so words in photos and scans are read in the background, newest first, and then search like any other text. It never gets in the way: one file at a time, with a pause between them, and only a passing note in the status line. A misread word is possible, so the interface says what cannot be found rather than implying everything can.

When a folder holds several saves of one document, the list marks the newest and the preview lists the others. Nothing is hidden or merged, because a name is only a clue and the person decides which version they want.

Folders open level by level, with a way back at the top, because that is how people already think about where they put things. A file that is renamed or moved keeps its tags, favorite and recent use: the same size and saved time tie the old record to the new one, and when two files match equally nothing is assumed.

The sidebar only says what is certain. A guessed category reads as a fact, and a few wrong guesses cost trust in everything else, so there are none: types come from the file extension and folders from the person's own structure. Tags are for anything else. Rows that would be empty are left out.

The deck opens on recent use, because what someone touched yesterday is the best guess at what they want now. The most recently used file sits in the middle. Up to five used files share the deck with new arrivals, so something that just came in is still seen. It rearranges when the window comes back, never under the pointer.

The arrival bar answers "I just saved that, where did it go?". It grows out of a small pill at the top of the screen when a new file lands, offers Copy and Open, and leaves after a few seconds. It is not always on screen, because something that is always there soon stops being seen. It never takes the keyboard, it stays away when the window is already in use, and it does not appear over a game, film or presentation. It also confirms a copy from quick search, because that bar has just disappeared.

First use is designed for a win within a minute: suggested folders that are read only after confirming, then a real word from the person's own photos to try, and only after a file has been used, the offer to start at sign-in. Each is offered once.

Collections are the one structure a person builds here, so they are lists over files rather than folders: a file can be in several, and nothing moves.

Roo exists because handing something over is easier than filing it. It is the one thing that stays on screen, so it earns that by being small, never taking the keyboard, and leaving during games and presentations. Dropping a file on it is the only place the app groups by subject, and that does not break the rule against guessed categories: the person asked, the answer comes straight back in words, the name is a word from the file, and a wrong result is one file in a list rather than a label on everything. Every mood is drawn in code and moved by the same function that moves a person's own picture, so a chosen image never feels like a downgrade.

Connected tools are off by default and split into reading and changing, because the person giving access should see exactly what they are giving. The warning that a tool may send text online sits beside the switch, and it stays there: this is the one place in the interface that states a data boundary, because here it is a risk rather than a claim.

See [docs/users.md](docs/users.md) for who this is for.

Finding a file is rarely the end of the job. A file can be dragged out or copied with `Ctrl C`, and both hand over a copy, so an original can never be moved by accident.

## References

The direction started from a dark card interface with a fanned deck, a smooth transition to a row and neighbours that make room on hover.

The 2026 revision studied [variant.com](https://variant.com) for its restraint: a near-black warm neutral, one off-white at reduced strengths, hairline borders around 10% white, regular-weight headlines, tight gutters and dashed placeholders. These are general principles. No Variant code, fonts or assets are used.

## Checking a change

Render the real widgets, then look at them. Check spacing, contrast, long filenames, the 1100 × 700 minimum window, the empty state and both languages. Windows is the reference platform, and screenshots from elsewhere use substitute fonts.

```powershell
python desktop_app.py --demo --render previews/windows/en-deck.png
python desktop_app.py --demo --view list --query "cognitive load" --select-first --render previews/windows/en-search.png
python desktop_app.py --demo --language zh --width 1100 --height 700 --render previews/windows/zh-narrow.png
```
