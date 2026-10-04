# app/parsers/structured_html_parser.py
"""HTML 解析器 —— 把政府政策网页等 HTML 转为结构化 Markdown。

为什么需要：政府政策（如税务总局公告）的正式发布形态是 HTML 网页而非 PDF，
而本项目原本只支持 PDF/DOCX/MD 等，导致政策类文档无法入库。

设计：复用 markdown/_pdf 解析器的「输出 Markdown 结构化文本」约定，
让下游的领域检测、分块器无需改动。使用 bs4 + lxml（已有依赖，不新增）。

结构保留策略：
- h1~h6 → 对应级别的 Markdown 标题（保层级，供 AST 分块用）
- p / div 文本 → 段落
- table → Markdown 表格
- li → 列表项
- script / style / nav / footer 等噪声标签整体剔除
"""
import asyncio
import re
from typing import List

from .base_parser import FileParserStrategy

# 与正文无关、需整体剔除的标签
_NOISE_TAGS = ("script", "style", "noscript", "nav", "footer", "header",
               "aside", "form", "iframe", "svg", "button")
# 常见的正文容器（按优先级尝试，命中即只解析该子树）
_MAIN_HINTS = ("article", "main", "content", "detail", "zoom", "TRS_Editor")


class StructuredHTMLParser(FileParserStrategy):
    """结构化 HTML 解析器：网页 → Markdown。"""

    def get_supported_mime_types(self) -> List[str]:
        return [
            "text/html",
            "application/xhtml+xml",
        ]

    async def parse(self, file_bytes: bytes) -> str:
        if not self.validate_file(file_bytes):
            raise ValueError("HTML 文件为空或无效")
        content = await asyncio.to_thread(self._extract, file_bytes)
        if not content.strip():
            raise ValueError("HTML 解析后内容为空")
        return content.strip()

    def _extract(self, file_bytes: bytes) -> str:
        from bs4 import BeautifulSoup
        html = self._decode(file_bytes)
        soup = BeautifulSoup(html, "lxml")

        # 剔除噪声标签
        for tag in soup(list(_NOISE_TAGS)):
            tag.decompose()

        # 尽量收窄到正文容器（避免导航/侧栏污染）
        root = self._pick_main(soup)

        lines: List[str] = []
        self._walk(root, lines)
        text = "\n".join(lines)
        # 压缩连续空行
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def _pick_main(self, soup):
        """挑选正文容器：优先 id/class 命中正文关键词的节点，否则用 body。"""
        for el in soup.find_all(["div", "article", "main"]):
            ident = " ".join(filter(None, [
                " ".join(el.get("class") or []), el.get("id") or ""
            ])).lower()
            if any(h.lower() in ident for h in _MAIN_HINTS):
                return el
        return soup.body or soup

    def _walk(self, node, lines: List[str]) -> None:
        """深度优先遍历，按标签类型输出 Markdown。"""
        from bs4 import NavigableString, Tag

        if isinstance(node, NavigableString):
            txt = str(node).strip()
            if txt:
                lines.append(txt)
            return

        if not isinstance(node, Tag):
            return

        name = node.name.lower()

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])
            txt = node.get_text(" ", strip=True)
            if txt:
                lines.append("")
                lines.append("#" * level + " " + txt)
                lines.append("")
            return

        if name == "table":
            table_md = self._table_to_md(node)
            if table_md:
                lines.append("")
                lines.append(table_md)
                lines.append("")
            return

        if name == "br":
            lines.append("")
            return

        if name == "li":
            txt = node.get_text(" ", strip=True)
            if txt:
                lines.append(f"- {txt}")
            return

        if name in ("p", "div", "section", "article", "td", "th", "blockquote"):
            # 段落类：若内部含块级元素则继续下钻，否则整段取出
            if node.find(["p", "div", "table", "h1", "h2", "h3", "ul", "ol", "section"]):
                for child in node.children:
                    self._walk(child, lines)
                return
            txt = node.get_text(" ", strip=True)
            if txt:
                lines.append("")
                lines.append(txt)
            return

        # 其它标签（span/a/strong 等）继续下钻
        for child in node.children:
            self._walk(child, lines)

    @staticmethod
    def _table_to_md(table) -> str:
        """HTML 表格 → Markdown 表格。"""
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True).replace("|", "\\|")
                     for c in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        header = "| " + " | ".join(rows[0]) + " |"
        sep = "|" + "|".join(["---"] * width) + "|"
        body = ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join([header, sep] + body)

    @staticmethod
    def _decode(file_bytes: bytes) -> str:
        """多编码尝试解码，并优先按 meta charset 声明的编码。"""
        head = file_bytes[:2048].decode("ascii", errors="ignore").lower()
        m = re.search(r'charset=["\']?([\w-]+)', head)
        encodings = []
        if m:
            encodings.append(m.group(1))
        encodings += ["utf-8", "gb18030", "gbk", "latin-1"]
        for enc in encodings:
            try:
                return file_bytes.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return file_bytes.decode("utf-8", errors="replace")
