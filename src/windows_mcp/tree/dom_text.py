"""Full-page text extraction from a browser window's DOM accessibility subtree.

Snapshot's tree traversal only keeps on-screen elements and is capped by the element
budget, so Scrape(use_dom=True) used to see just the visible viewport. This module
fetches every text element under the page's RootWebArea in one UIA round-trip
(FindAllBuildCache over TreeScope_Descendants), regardless of scroll position. That
includes same-process iframe documents, which Chromium nests under the main page.
Content removed from the accessibility tree (display:none, closed <details>) is still
not visible to UIA and cannot be returned.
"""

from __future__ import annotations

import logging

from windows_mcp.uia import (
    CacheRequest,
    ControlFromHandle,
    ControlType,
    CreatePropertyCondition,
    PropertyId,
    TreeScope,
)

logger = logging.getLogger(__name__)

#: Hard cap on returned characters so a pathological page cannot flood the response.
MAX_PAGE_TEXT_CHARS = 200_000


def extract_page_text(handle: int) -> str | None:
    """Return all DOM text of the browser window ``handle``, in document order.

    Args:
        handle: Native window handle of a browser top-level window.

    Returns:
        The page text joined by newlines (consecutive duplicate lines collapsed), or
        None when the window exposes no RootWebArea.
    """
    window = ControlFromHandle(handle)
    if not window:
        return None
    root = window.FindFirst(
        TreeScope.TreeScope_Descendants,
        CreatePropertyCondition(PropertyId.AutomationIdProperty, "RootWebArea"),
    )
    if not root:
        return None

    cache_request = CacheRequest()
    cache_request.TreeScope = TreeScope.TreeScope_Element
    cache_request.AddProperty(PropertyId.NameProperty)
    text_elements = root.FindAllBuildCache(
        TreeScope.TreeScope_Descendants,
        CreatePropertyCondition(PropertyId.ControlTypeProperty, ControlType.TextControl),
        cache_request,
    )

    lines: list[str] = []
    total = 0
    for element in text_elements:
        try:
            text = element.CachedName.strip()
        except Exception:
            continue
        # Chromium exposes headings and their inner static text as nested TextControls
        # with the same name; drop the immediate repeat.
        if not text or (lines and lines[-1] == text):
            continue
        lines.append(text)
        total += len(text) + 1
        if total >= MAX_PAGE_TEXT_CHARS:
            lines.append(f"[truncated at {MAX_PAGE_TEXT_CHARS} characters]")
            break
    logger.debug("DOM page text: %d text elements, %d lines", len(text_elements), len(lines))
    return "\n".join(lines)
