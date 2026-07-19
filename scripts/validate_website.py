#!/usr/bin/env python3
"""Validate the self-contained static project website."""

from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"] or "")
        if tag in {"a", "link", "script", "img"}:
            target = values.get("href") or values.get("src")
            if target:
                self.links.append(target)


def local_target(root: Path, page: Path, link: str) -> tuple[Path, str] | None:
    parsed = urlsplit(link)
    if parsed.scheme or parsed.netloc or link.startswith(("mailto:", "tel:", "data:")):
        return None
    if parsed.path in {"", "/"}:
        target = root / "index.html" if parsed.path == "/" else page
    elif parsed.path.startswith("/"):
        target = root / parsed.path.lstrip("/")
    else:
        target = page.parent / parsed.path
    return target.resolve(), parsed.fragment


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "website"
    errors: list[str] = []
    page_ids: dict[Path, set[str]] = {}
    page_links: dict[Path, list[str]] = {}

    for page in sorted(root.glob("*.html")):
        parser = PageParser()
        try:
            parser.feed(page.read_text(encoding="utf-8"))
            parser.close()
        except Exception as exc:  # pragma: no cover - diagnostic path
            errors.append(f"{page.name}: cannot parse HTML: {exc}")
            continue
        duplicates = sorted({value for value in parser.ids if parser.ids.count(value) > 1})
        if duplicates:
            errors.append(f"{page.name}: duplicate ids: {', '.join(duplicates)}")
        page_ids[page.resolve()] = set(parser.ids)
        page_links[page.resolve()] = parser.links

    for page, links in page_links.items():
        for link in links:
            resolved = local_target(root, page, link)
            if resolved is None:
                continue
            target, fragment = resolved
            if not target.is_file():
                errors.append(f"{page.name}: missing local target {link}")
                continue
            if fragment and target.suffix.lower() == ".html":
                target_ids = page_ids.get(target)
                if target_ids is None:
                    parser = PageParser()
                    parser.feed(target.read_text(encoding="utf-8"))
                    target_ids = set(parser.ids)
                if fragment not in target_ids:
                    errors.append(f"{page.name}: missing fragment target {link}")

    if errors:
        print("Website validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Website validation passed: {len(page_links)} HTML pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
