"""Extract issue identity and article cards without executing page scripts."""

import re
import json
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.models.article import Article, ArticleSummary
from app.models.audio import RelatedAudio
from app.models.issue import Issue
from app.newyorker.archive import issue_url, issue_cover_url
from app.newyorker.images import safe_image_url
from app.newyorker.client import SITE_HOSTS
from pydantic import ValidationError


class ParseError(Exception):
    """A safe page-layout error without HTML, URLs, or account values."""


class StateScripts(HTMLParser):
    """Collect inline scripts as text; do not interpret or execute JavaScript."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.current: list[str] | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


def extract_preloaded_state(html: str) -> dict:
    states = []
    try:
        for script in StateScripts(html).scripts:
            for match in re.finditer(r"(?:\bwindow\.)?__PRELOADED_STATE__\s*=\s*", script):
                value, end = json.JSONDecoder().raw_decode(script[match.end():])
                tail = script[match.end() + end:].lstrip()
                if not isinstance(value, dict) or tail and not tail.startswith(";"):
                    raise ValueError
                states.append(value)
    except (ValueError, RecursionError) as error:
        raise ParseError("Article state is not supported JSON.") from error
    if len(states) != 1:
        raise ParseError("Article page must contain one structured application state.")
    return states[0]


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str]
    parent: "Node | None" = None
    children: list["Node | str"] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self) -> str:
        if self.tag in ("script", "style", "noscript"):
            return ""
        return " ".join(" ".join(child.text() if isinstance(child, Node) else child for child in self.children).split())


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {key: value or "" for key, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_issue_identity(html: str, requested_url: str) -> Issue:
    document = Document(html)
    title = next((node.text() for node in document.root.walk() if node.tag == "title"), "")
    label = title.removesuffix(" | The New Yorker").removeprefix("The New Yorker ")
    match = re.search(r"\b([A-Z][a-z]+) (\d{1,2})(?:\s*&\s*\d{1,2})?, (\d{4})\b", label)
    if not match:
        raise ParseError("Issue page has no recognizable issue date.")
    try:
        issue_date = datetime.strptime(" ".join(match.groups()), "%B %d %Y").date()
    except ValueError as error:
        raise ParseError("Issue page has an unsupported issue date.") from error
    target = issue_url(requested_url)
    if target is None:
        parsed = urlsplit(requested_url)
        if parsed.scheme != "https" or parsed.hostname not in SITE_HOSTS or parsed.path.rstrip("/") != "/magazine":
            raise ParseError("Expected a magazine issue page.")
        url = f"https://www.newyorker.com/magazine/{issue_date:%Y/%m/%d}"
    else:
        url, expected_date = target
        if expected_date != issue_date:
            raise ParseError("Issue page date differs from the requested issue.")
    return Issue(url=url, issue_date=issue_date, label=label, cover_image_url=issue_cover_url(html))


EDITORIAL_ROOTS = {"magazine", "news", "culture", "humor", "fiction", "poems", "books", "cartoons", "gallery", "puzzles-and-games", "puzzles-and-games-dept"}


def article_url(href: str, base: str) -> str | None:
    try:
        parsed = urlsplit(urljoin(base, href))
        parts = [part for part in parsed.path.split("/") if part]
        if (parsed.scheme != "https" or parsed.hostname not in SITE_HOSTS or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or len(parts) < 2
                or parts[0] not in EDITORIAL_ROOTS):
            return None
        if parts[0] == "magazine" and not re.fullmatch(r"/magazine/\d{4}/\d{2}/\d{2}/[^/]+/?", parsed.path):
            return None
        return urlunsplit(("https", "www.newyorker.com", parsed.path.rstrip("/"), "", ""))
    except ValueError:
        return None


def primary_article(state: dict, requested_url: str) -> dict:
    """Select the captured primary article; never search recommendation metadata."""
    transformed = state.get("transformed")
    if not isinstance(transformed, dict) or not isinstance(transformed.get("article"), dict):
        raise ParseError("Page has no supported primary article metadata.")
    canonical = transformed.get("head.canonicalUrl")
    target = article_url(requested_url, requested_url)
    if not isinstance(canonical, str) or not target or article_url(canonical, requested_url) != target:
        raise ParseError("Primary article identity differs from the requested article.")
    return transformed["article"]


def extract_article(state: dict, requested_url: str) -> Article:
    primary = primary_article(state, requested_url)
    header = primary.get("headerProps")
    if not isinstance(header, dict):
        raise ParseError("Primary article has no supported header metadata.")
    headline = header.get("dangerousHed")
    identifier = primary.get("id")
    if not isinstance(headline, str) or not isinstance(identifier, str) or not identifier.strip():
        raise ParseError("Primary article is missing its headline or ID.")
    title = Document(headline).root.text()
    if not title:
        raise ParseError("Primary article has an empty headline.")
    description = header.get("dangerousDek")
    authors = []
    contributors = header.get("contributors", {})
    if isinstance(contributors, dict):
        author_group = contributors.get("author", {})
        if isinstance(author_group, dict) and isinstance(author_group.get("items"), list):
            authors = [item["name"].strip() for item in author_group["items"]
                       if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()]
    return Article(id=identifier, title=title, url=article_url(requested_url, requested_url),
                   description=Document(description).root.text() if isinstance(description, str) else None,
                   authors=list(dict.fromkeys(authors)))


def extract_related_audio(state: dict, requested_url: str) -> RelatedAudio | None:
    primary = primary_article(state, requested_url)
    if "relatedAudio" not in primary:
        raise ParseError("Primary article has no explicit narration metadata field.")
    related = primary["relatedAudio"]
    if related is None:
        return None
    if not isinstance(related, dict):
        raise ParseError("Primary article narration metadata has an unsupported shape.")
    try:
        return RelatedAudio.model_validate(related)
    except ValidationError as error:
        raise ParseError("Primary article narration metadata is incomplete or invalid.") from error


def class_has(node: Node, pattern: str) -> bool:
    return bool(re.search(pattern, node.attrs.get("class", ""), re.I))


def card_for(node: Node) -> Node | None:
    current = node
    while current is not None:
        if current.tag in ("nav", "header", "footer"):
            return None
        if current.tag == "article" or class_has(current, r"(?:^|\s)(?:SummaryItemWrapper[^\s]*|summary-item(?:__wrapper)?|summary-item-wrapper|summaryitem-wrapper)(?:\s|$)"):
            if current.tag == "article":
                destinations = {article_url(child.attrs.get("href", ""), "https://www.newyorker.com")
                                for child in current.walk() if child.tag == "a"}
                destinations.discard(None)
                if len(destinations) != 1:
                    return None
            # Ignore cards inside navigation or the page footer.
            ancestor = current.parent
            while ancestor:
                if ancestor.tag in ("nav", "header", "footer"):
                    return None
                ancestor = ancestor.parent
            return current
        current = current.parent
    return None


def first_text(nodes: list[Node], pattern: str) -> str | None:
    return next((node.text() for node in nodes if class_has(node, pattern) and node.text()), None)


def parse_issue_articles(html: str, issue: Issue) -> list[ArticleSummary]:
    actual = parse_issue_identity(html, issue.url)
    if actual.issue_date != issue.issue_date:
        raise ParseError("Issue metadata does not match the selected issue.")
    document = Document(html)
    articles: dict[str, ArticleSummary] = {}
    section = None
    for node in document.root.walk():
        if node.tag == "h2" and card_for(node) is None and node.text():
            section = node.text()
        if node.tag != "a":
            continue
        url = article_url(node.attrs.get("href", ""), issue.url)
        card = card_for(node)
        if url is None or card is None:
            continue
        nodes = list(card.walk())
        title = next((child.text() for child in node.walk()
                      if child.tag in ("h2", "h3", "h4") or class_has(child, r"SummaryItemHed|summary-item__hed")), None)
        title = title or node.text()
        if not title or title.casefold() in ("listen", "listen now", "read more", "save", "save story"):
            continue
        description = first_text(nodes, r"SummaryItemDek|summary-item__dek")
        byline = first_text(nodes, r"BylineWrapper|byline(?:[-_ ]|$)")
        if byline is None:
            byline = next((child.text() for child in nodes if child.tag in ("p", "span", "div")
                           and child.text().startswith("By ") and len(child.text()) < 1000), None)
        if description is None:
            description = next((child.text() for child in nodes if child.tag == "p" and child.text()
                                and not child.text().startswith("By ")), None)
        image = next((safe_image_url(child.attrs.get("src", "") or child.attrs.get("data-src", ""))
                      for child in nodes if child.tag == "img" and safe_image_url(child.attrs.get("src", "") or child.attrs.get("data-src", ""))), None)
        listen = any(child.text().casefold() in ("listen", "listen to this article", "listen now") for child in nodes)
        entry = ArticleSummary(title=title, url=url, section=section,
                               rubric=first_text(nodes, r"SummaryItemRubric|summary-item__rubric"),
                               byline=byline, description=description, image_url=image,
                               audio_available=True if listen else None)
        if url not in articles:
            articles[url] = entry
    if not articles:
        raise ParseError("Issue page has no recognized article cards. A private page capture is available for layout inspection.")
    return list(articles.values())
