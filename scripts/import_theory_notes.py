#!/usr/bin/env python3
"""把「财经七书学习讲义」Markdown 拆成 Obsidian vault 笔记。

源文件是一份 654KB 的长 md（前言 + 七个部分 + 例题精解 + 附录），按
`# 第X部分` / `## 模块 N` 两级标题切分，每个模块落成一篇独立笔记，
写入 vault 的 09-理论讲义/<部分>/ 目录，供 scripts/build_vault_index.py
索引（meta.category = 09-理论讲义，meta.path 含书名副目录，可被
tdx-vault.search 的 category / path_prefix 过滤）。

用法：
    python scripts/import_theory_notes.py                     # 默认源与目标
    python scripts/import_theory_notes.py --src X.md --out DIR --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_SRC = Path("/home/jiuben/桌面/财经七书学习讲义_模块精讲版.md")
DEFAULT_OUT = Path("/home/jiuben/ObsidianVaults/tdx-data-feed/09-理论讲义")

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
          "七": 7, "八": 8, "九": 9, "十": 10}

# 书名 → 目录序号（与讲义顺序一致）
PART_RE = re.compile(r"^第([一二三四五六七八九十]+)部分\s*(.+)$")

# 书名 → 能把该书理论落到本仓数据上的技能。写进 frontmatter.related_skills，
# build_vault_index.py 会提进 meta，theory-reference 检索时一并回传给调用方。
BOOK_SKILLS = {
    "投资学": ["kline-analysis", "indicator-match", "backtest"],
    "公司理财": ["financial-summary", "financial-statement"],
    "会计学": ["financial-statement", "financial-summary"],
    "宏观经济学": ["news-summary"],
    "货币金融学": ["news-summary", "financial-summary"],
    "金融市场与金融机构": ["news-summary", "kline-analysis"],
    "计量经济学": ["backtest", "indicator-match"],
    "例题精解与实操补充": ["financial-summary", "backtest"],
    "前言": [],
    "附录": [],
}

SKILL_DESC = {
    "kline-analysis": "K线形态与走势分析",
    "indicator-match": "MA / MACD / KDJ / RSI 指标打分",
    "financial-summary": "营收 / 净利 / ROE / 毛利率 / EPS / PE / PB 摘要",
    "financial-statement": "三大报表原始字段查询",
    "news-summary": "个股新闻、公告与市场要闻",
    "backtest": "单股策略 / 信号回测",
    "adj-factor": "复权因子与除权除息事件",
}


def slug(text: str) -> str:
    """标题 → 安全文件名片段。"""
    s = text.strip()
    s = s.replace("　", " ").replace("\u3000", " ")
    s = re.sub(r"[：:]", "-", s)
    s = re.sub(r"[（(]", "-", s)
    s = re.sub(r"[）)]", "", s)
    s = re.sub(r"[·•]", "-", s)
    s = re.sub(r"[\\/:*?\"<>|]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:60]


def split_parts(text: str) -> list[tuple[str, str]]:
    """按一级标题切成 [(part_title, part_body), ...]"""
    lines = text.split("\n")
    parts: list[tuple[str, list[str]]] = []
    cur_title, cur_body = None, []
    for ln in lines:
        if re.match(r"^#\s+\S", ln):
            if cur_title is not None:
                parts.append((cur_title, cur_body))
            cur_title, cur_body = ln[2:].strip(), []
        elif cur_title is not None:
            cur_body.append(ln)
    if cur_title is not None:
        parts.append((cur_title, cur_body))
    return [(t, "\n".join(b).strip()) for t, b in parts]


def split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """部分正文 → (部分导语, [(二级标题, 内容), ...])"""
    lines = body.split("\n")
    intro: list[str] = []
    secs: list[tuple[str, list[str]]] = []
    cur_title, cur = None, []
    for ln in lines:
        if re.match(r"^##\s+\S", ln):
            if cur_title is not None:
                secs.append((cur_title, cur))
            cur_title, cur = ln[3:].strip(), []
        elif cur_title is None:
            intro.append(ln)
        else:
            cur.append(ln)
    if cur_title is not None:
        secs.append((cur_title, cur))
    return "\n".join(intro).strip(), [(t, "\n".join(b).strip()) for t, b in secs]


def part_dir(part_title: str) -> tuple[str, str]:
    """一级标题 → (目录名, 书名)"""
    if part_title.startswith("前言"):
        return "00-前言", "前言"
    if part_title.startswith("附录"):
        return "09-附录", "附录"
    m = PART_RE.match(part_title)
    if m:
        n = CN_NUM.get(m.group(1), 0)
        book = re.sub(r"\s*·\s*模块精讲\s*$", "", m.group(2)).strip()
        book = re.sub(r"（.*?）\s*$", "", book).strip()
        return f"{n:02d}-{slug(book)}", book
    return f"99-{slug(part_title)}", part_title


def note_name(sec_title: str, book: str, idx: int) -> str:
    m = re.match(r"^模块\s*(\d+)\s*(.*)$", sec_title)
    if m:
        return f"模块{int(m.group(1)):02d}-{slug(m.group(2))}.md"
    return f"{idx:02d}-{slug(sec_title)}.md"


def frontmatter(title: str, book: str, part: str, tags: list[str],
                skills: list[str] | None = None) -> str:
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    skill_block = ""
    if skills:
        skill_lines = "\n".join(f"  - {s}" for s in skills)
        skill_block = f"related_skills:\n{skill_lines}\n"
    return (
        "---\n"
        f"title: {title}\n"
        "category: 09-理论讲义\n"
        f"book: {book}\n"
        f"part: {part}\n"
        "tags:\n"
        f"{tag_lines}\n"
        f"{skill_block}"
        "source: 财经七书学习讲义_模块精讲版.md\n"
        "---\n"
    )


def skills_line(skills: list[str]) -> str:
    """模块笔记抬头的一行提示：概念读完后可以用哪些技能落到本仓数据上。"""
    if not skills:
        return ""
    return " ｜ 相关技能：" + "、".join(f"`{s}`" for s in skills)


def skills_section(book: str, skills: list[str]) -> str:
    """部分总览里的「相关技能」小节。"""
    if not skills:
        return ""
    rows = "\n".join(
        f"- `{s}` — {SKILL_DESC.get(s, '')}" for s in skills)
    return (f"\n## 相关技能\n\n《{book}》的理论可以用以下技能落到本仓数据上：\n\n{rows}\n")


def build(src: Path, out: Path, dry_run: bool) -> dict:
    text = src.read_text(encoding="utf-8")
    parts = split_parts(text)
    written: list[str] = []
    toc: list[str] = []

    for part_title, part_body in parts:
        dirname, book = part_dir(part_title)
        skills = BOOK_SKILLS.get(book, [])
        intro, secs = split_sections(part_body)
        pdir = out / dirname
        if not dry_run:
            pdir.mkdir(parents=True, exist_ok=True)

        sec_names = []
        for i, (sec_title, sec_body) in enumerate(secs, start=1):
            fname = note_name(sec_title, book, i)
            full_title = f"{book} · {sec_title}" if book not in ("前言", "附录") else sec_title
            tags = ["财经七书", book, "讲义"]
            m = re.match(r"^模块\s*(\d+)", sec_title)
            if m:
                tags.append(f"模块{m.group(1)}")
            content = (
                frontmatter(full_title, book, part_title, tags, skills)
                + f"\n# {full_title}\n\n"
                + f"> 所属：{part_title} ｜ 源文件：{src.name}{skills_line(skills)}\n\n"
                + sec_body
                + "\n"
            )
            path = pdir / fname
            if not dry_run:
                path.write_text(content, encoding="utf-8")
            written.append(str(path.relative_to(out.parent.parent)))
            sec_names.append((fname[:-3], sec_title))

        # 部分总览（导语 + 小节链接）
        links = "\n".join(f"- [[{name}｜{title}]]" for name, title in sec_names)
        overview = (
            frontmatter(f"{book} · 总览", book, part_title,
                        ["财经七书", book, "总览"], skills)
            + f"\n# {book} · 总览\n\n"
            + f"> 所属：{part_title} ｜ 源文件：{src.name}\n\n"
            + (intro + "\n\n" if intro else "")
            + "## 本节笔记\n\n" + links + "\n"
            + skills_section(book, skills)
        )
        opath = pdir / "00-总览.md"
        if not dry_run:
            opath.write_text(overview, encoding="utf-8")
        written.append(str(opath.relative_to(out.parent.parent)))
        toc.append((dirname, book, len(sec_names), skills))

    # 全局总览
    toc_lines = "\n".join(
        f"- [[{d}/00-总览｜{b}]]（{n} 篇"
        + (f" · 相关技能 {'、'.join(sk)}" if sk else "") + "）"
        for d, b, n, sk in toc)
    index = (
        frontmatter("财经七书学习讲义 · 总览", "财经七书", "全部",
                    ["财经七书", "总览", "讲义"])
        + "\n# 财经七书学习讲义 · 总览\n\n"
        + f"> 源文件：{src.name}（{src.stat().st_size/1024:.0f} KB）\n"
        + "> 用途：概念/理论检索。`tdx-vault.search` 加 `category: 09-理论讲义`\n"
        + "> 可只在本库内检索；加 `path_prefix: 09-理论讲义/01-投资学` 可限定单本书。\n"
        + "> 每篇笔记的 frontmatter 带 `related_skills`，索引后进 `meta`，\n"
        + "> `theory-reference` 会随命中片段一并回传，便于把理论接到实盘数据技能上。\n\n"
        + "## 分册\n\n" + toc_lines + "\n"
    )
    ipath = out / "00-讲义总览.md"
    if not dry_run:
        ipath.write_text(index, encoding="utf-8")
    written.append(str(ipath.relative_to(out.parent.parent)))

    return {"notes": len(written), "parts": len(toc), "dry_run": dry_run,
            "out": str(out),
            "linked_skills": sum(1 for *_, sk in toc if sk),
            "toc": [{"dir": d, "book": b, "sections": n, "related_skills": sk}
                    for d, b, n, sk in toc]}


def main() -> int:
    ap = argparse.ArgumentParser(description="导入财经七书讲义到 vault")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.src.exists():
        print(f"源文件不存在：{args.src}", file=sys.stderr)
        return 1
    stats = build(args.src, args.out, args.dry_run)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
