from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

START_URL = "https://www.partio.fi/pestihaku/"
APPLY_URL_PREFIX = "https://kuksa.partio.fi/Kotisivut/login.aspx?PREId="
ACCEPT_LANGUAGE = "fi-FI,fi;q=0.9,en-US;q=0.8,en;q=0.7"
REQUEST_TIMEOUT_SECONDS = 30
VERSION = "1.0.0"
DEFAULT_OUTPUT_FILE = Path("posts.json")
LISTING_CACHE_BYPASS_HEADERS = {
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
LIST_TAGS = {"ul", "ol"}
STRUCTURAL_TAGS = {
    "article",
    "aside",
    "blockquote",
    "div",
    "figure",
    "figcaption",
    "footer",
    "header",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "thead",
    "tr",
    "td",
    "th",
}
SKIPPED_TAGS = {"script", "style", "noscript", "iframe", "svg", "path"}

REQUIRED_SECTION_HEADINGS = (
    "pestissä tarvittavat taidot tai kokemus",
    "pestissä tarvittavat taidot ja kokemus",
)
ADDITIONAL_SECTION_HEADINGS = ("lisätiedot", "lisätietoja")


def main() -> None:
    args = parse_args()
    session = requests.Session()
    session.headers.update({"Accept-Language": ACCEPT_LANGUAGE})

    listing_urls = sorted(collect_listing_urls(session, args.start_url))
    postings = [extract_posting(session, url) for url in listing_urls]

    payload = {
        "fetched": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": VERSION,
        "count": len(postings),
        "listings": postings,
    }
    write_json(payload, args.output)

    print(f"Found {len(postings)} postings")
    print(f"Results written to {args.output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape partio.fi pestihakuilmoitukset")
    parser.add_argument(
        "--start-url",
        default=START_URL,
        help="Listing page URL to start from",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help="Output JSON file path",
    )
    return parser.parse_args()


def collect_listing_urls(session: requests.Session, start_url: str) -> set[str]:
    listing_urls: set[str] = set()
    visited_pages: set[str] = set()
    page_url = normalize_content_url(start_url)

    while page_url and page_url not in visited_pages:
        visited_pages.add(page_url)
        soup = get_soup(session, page_url, headers=LISTING_CACHE_BYPASS_HEADERS)

        for link in soup.select("#events-search-results-content article.enlistment-item a[href]"):
            href = str(link.get("href", "")).strip()
            if not href:
                continue
            absolute = absolute_url(page_url, href)
            normalized = normalize_content_url(absolute)
            if is_enlistment_url(normalized):
                listing_urls.add(normalized)

        page_url = extract_next_listing_page_url(soup, page_url, start_url)

    return listing_urls


def extract_next_listing_page_url(
    soup: BeautifulSoup, page_url: str, listing_root_url: str
) -> str:
    next_link = soup.select_one("#events-search-load-more a[href]")
    if next_link is None:
        return ""

    href = str(next_link.get("href", "")).strip()
    if not href:
        return ""

    next_page = normalize_content_url(absolute_url(page_url, href))
    if not is_listing_page_url(next_page, listing_root_url):
        return ""
    return next_page


def extract_posting(session: requests.Session, url: str) -> dict:
    soup = get_soup(session, url)

    entry_content, required_skills, additional_info = extract_content_sections(soup, url)
    return {
        "url": url,
        "apply_url": extract_apply_url(soup, url),
        "job_title": extract_job_title(soup, url),
        "entry_meta": extract_entry_meta(soup, url),
        "entry_content": entry_content,
        "required_skills_or_experience": required_skills,
        "additional_info": additional_info,
    }


def extract_job_title(soup: BeautifulSoup, page_url: str) -> str:
    title_element = soup.select_one("h1.entry-title")
    if title_element is None:
        return ""
    return normalize_block_text(render_inline_fragment(title_element, page_url))


def extract_entry_meta(soup: BeautifulSoup, page_url: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in soup.select("div.entry-meta div.entry-meta-item"):
        label_element = item.select_one("h1, h2, h3, h4, h5, h6")
        if label_element is None:
            continue

        label = normalize_block_text(render_inline_fragment(label_element, page_url))
        if not label:
            continue

        blocks: list[str] = []
        for child in item.children:
            if child is label_element:
                continue
            if isinstance(child, Tag) and child.name in HEADING_TAGS:
                continue
            span_as_block = isinstance(child, Tag) and child.name == "span"
            blocks.extend(render_node_blocks(child, page_url, span_as_block=span_as_block))

        lines: list[str] = []
        for block in blocks:
            for line in block.splitlines():
                cleaned = line.strip()
                if cleaned:
                    lines.append(cleaned)

        if lines:
            result[label] = "\n".join(lines)

    return result


def extract_content_sections(soup: BeautifulSoup, page_url: str) -> tuple[str, str, str]:
    container = soup.select_one("div.entry-content")
    if container is None:
        return "", "", ""

    entry_content_blocks: list[str] = []
    required_blocks: list[str] = []
    additional_blocks: list[str] = []
    current_section = ""

    for node in container.children:
        if isinstance(node, Tag) and node.name in HEADING_TAGS:
            heading_text = normalize_block_text(render_inline_fragment(node, page_url))
            if not heading_text:
                continue

            if is_required_heading(heading_text):
                current_section = "required"
                continue
            if is_additional_heading(heading_text):
                current_section = "additional"
                continue

            if current_section == "required":
                required_blocks.append(heading_text)
            elif current_section == "additional":
                additional_blocks.append(heading_text)
            else:
                entry_content_blocks.append(heading_text)
            continue

        span_as_block = isinstance(node, Tag) and node.name == "span"
        node_blocks = render_node_blocks(node, page_url, span_as_block=span_as_block)
        for block in node_blocks:
            if not block:
                continue
            if current_section == "required":
                required_blocks.append(block)
            elif current_section == "additional":
                additional_blocks.append(block)
            else:
                entry_content_blocks.append(block)

    return (
        join_blocks(entry_content_blocks),
        join_blocks(required_blocks),
        join_blocks(additional_blocks),
    )


def extract_apply_url(soup: BeautifulSoup, page_url: str) -> str:
    entry_content = soup.select_one("div.entry-content")
    if entry_content is not None:
        for link in entry_content.select("a[href]"):
            href = str(link.get("href", "")).strip()
            if not href:
                continue
            absolute = absolute_url(page_url, href)
            if is_apply_url(absolute):
                return absolute

    for link in soup.select("a[href]"):
        href = str(link.get("href", "")).strip()
        if not href:
            continue
        absolute = absolute_url(page_url, href)
        if is_apply_url(absolute):
            return absolute
    return ""


def render_node_blocks(node: object, page_url: str, *, span_as_block: bool) -> list[str]:
    if isinstance(node, NavigableString):
        text = normalize_block_text(str(node))
        return [text] if text else []
    if not isinstance(node, Tag):
        return []
    if node.name in SKIPPED_TAGS or node.name == "br":
        return []
    if node.name == "a" and is_apply_link_tag(node, page_url):
        return []

    if node.name in LIST_TAGS:
        block = render_list_block(node, page_url)
        return [block] if block else []
    if node.name in HEADING_TAGS:
        text = normalize_block_text(render_inline_fragment(node, page_url))
        return [text] if text else []
    if node.name == "span" and not span_as_block and not contains_structural_descendant(node):
        text = normalize_block_text(render_inline_fragment(node, page_url))
        return [text] if text else []

    blocks = collect_child_blocks(node, page_url)
    if blocks:
        return blocks

    text = normalize_block_text(render_inline_fragment(node, page_url))
    return [text] if text else []


def collect_child_blocks(tag: Tag, page_url: str) -> list[str]:
    blocks: list[str] = []
    inline_buffer: list[str] = []

    def flush_inline_buffer() -> None:
        text = normalize_block_text("".join(inline_buffer))
        inline_buffer.clear()
        if text:
            blocks.append(text)

    for child in tag.children:
        if isinstance(child, NavigableString):
            inline_buffer.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in SKIPPED_TAGS:
            continue
        if child.name == "br":
            inline_buffer.append("\n")
            continue
        if child.name == "a" and is_apply_link_tag(child, page_url):
            continue

        if is_structural_child(child):
            flush_inline_buffer()
            blocks.extend(render_node_blocks(child, page_url, span_as_block=False))
            continue

        inline_buffer.append(render_inline_fragment(child, page_url))

    flush_inline_buffer()
    return blocks


def is_structural_child(tag: Tag) -> bool:
    if tag.name in HEADING_TAGS or tag.name in LIST_TAGS or tag.name in STRUCTURAL_TAGS:
        return True
    return tag.name == "span" and contains_structural_descendant(tag)


def contains_structural_descendant(tag: Tag) -> bool:
    for descendant in tag.descendants:
        if not isinstance(descendant, Tag) or descendant is tag:
            continue
        if (
            descendant.name in HEADING_TAGS
            or descendant.name in LIST_TAGS
            or descendant.name in STRUCTURAL_TAGS
        ):
            return True
    return False


def render_list_block(list_tag: Tag, page_url: str) -> str:
    lines = render_list_lines(list_tag, page_url, depth=0)
    return "\n".join(lines)


def render_list_lines(list_tag: Tag, page_url: str, depth: int) -> list[str]:
    lines: list[str] = []
    for list_item in list_tag.find_all("li", recursive=False):
        item_text = render_list_item_text(list_item, page_url)
        if item_text:
            lines.extend(prefix_list_item(item_text, depth))

        for nested_list in list_item.find_all(["ul", "ol"], recursive=False):
            lines.extend(render_list_lines(nested_list, page_url, depth + 1))
    return lines


def render_list_item_text(list_item: Tag, page_url: str) -> str:
    parts: list[str] = []
    for child in list_item.children:
        if isinstance(child, Tag) and child.name in LIST_TAGS:
            continue
        parts.append(render_inline_fragment(child, page_url))
    return normalize_block_text("".join(parts))


def prefix_list_item(text: str, depth: int) -> list[str]:
    indent = "  " * depth
    lines = text.splitlines()
    if not lines:
        return []

    prefixed = [f"{indent}- {lines[0]}"]
    for line in lines[1:]:
        prefixed.append(f"{indent}  {line}" if line else "")
    return prefixed


def render_inline_fragment(node: object, page_url: str) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    if node.name in SKIPPED_TAGS:
        return ""
    if node.name == "br":
        return "\n"
    if node.name in LIST_TAGS:
        return ""
    if node.name == "a":
        return render_anchor(node, page_url)
    return "".join(render_inline_fragment(child, page_url) for child in node.children)


def render_anchor(anchor: Tag, page_url: str) -> str:
    href = str(anchor.get("href", "")).strip()
    if href and is_apply_url(absolute_url(page_url, href)):
        return ""

    label = normalize_block_text("".join(render_inline_fragment(child, page_url) for child in anchor.children))
    if not href:
        return label

    absolute = absolute_url(page_url, href)
    if not label:
        return absolute
    if label == absolute:
        return absolute
    return f"{label} ({absolute})"


def join_blocks(blocks: list[str]) -> str:
    normalized_blocks: list[str] = []
    for block in blocks:
        normalized = normalize_block_text(block)
        if normalized:
            normalized_blocks.append(normalized)
    return "\n\n".join(normalized_blocks)


def normalize_block_text(value: str) -> str:
    value = value.replace("\r", "\n").replace("\xa0", " ")

    cleaned_lines: list[str] = []
    previous_blank = False
    for raw_line in value.split("\n"):
        cleaned = re.sub(r"\s+", " ", raw_line).strip()
        if cleaned.startswith(("•", "●", "▪", "◦")):
            cleaned = f"- {cleaned.lstrip('•●▪◦').strip()}"
        if not cleaned:
            if cleaned_lines and not previous_blank:
                cleaned_lines.append("")
            previous_blank = True
            continue
        cleaned_lines.append(cleaned)
        previous_blank = False

    while cleaned_lines and cleaned_lines[0] == "":
        cleaned_lines.pop(0)
    while cleaned_lines and cleaned_lines[-1] == "":
        cleaned_lines.pop()

    return "\n".join(cleaned_lines)


def is_required_heading(heading: str) -> bool:
    normalized = normalize_heading(heading)
    return any(normalized.startswith(candidate) for candidate in REQUIRED_SECTION_HEADINGS)


def is_additional_heading(heading: str) -> bool:
    normalized = normalize_heading(heading)
    return any(normalized.startswith(candidate) for candidate in ADDITIONAL_SECTION_HEADINGS)


def normalize_heading(heading: str) -> str:
    return " ".join(heading.casefold().split())


def get_soup(
    session: requests.Session, url: str, headers: dict[str, str] | None = None
) -> BeautifulSoup:
    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=headers)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def write_json(data: dict, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_enlistment_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.netloc == "www.partio.fi" and parsed.path.startswith("/enlistment/")


def is_listing_page_url(url: str, listing_root_url: str) -> bool:
    path = normalized_path(url)
    listing_root = normalized_path(listing_root_url)
    return path == listing_root or path.startswith(f"{listing_root}/page/")


def normalize_content_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def absolute_url(base_url: str, href: str) -> str:
    parsed = urlsplit(urljoin(base_url, href))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def normalized_path(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path or "/"


def is_apply_link_tag(anchor: Tag, page_url: str) -> bool:
    href = str(anchor.get("href", "")).strip()
    if not href:
        return False
    return is_apply_url(absolute_url(page_url, href))


def is_apply_url(url: str) -> bool:
    return url.startswith(APPLY_URL_PREFIX)


if __name__ == "__main__":
    main()
