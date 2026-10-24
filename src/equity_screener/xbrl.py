"""Parse one filing's XBRL instance document, keeping the dimensions of each fact.

The companyfacts API drops every fact reported with a dimension and every company-specific
tag. The instance document has them all. Pure: bytes in, plain dicts out.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from io import BytesIO

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
TEXT_TAXONOMIES = ("dei",)  # non-numeric facts are kept only from these
MAX_TEXT_LENGTH = 200


class InstanceError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _namespace(tag: str) -> str:
    return tag[1:].partition("}")[0] if tag.startswith("{") else ""


def _measure(text: str | None) -> str:
    return (text or "").strip().rpartition(":")[2]


def _context(node: ET.Element) -> dict | None:
    start = end = None
    dims: list[list[str]] = []
    for child in node.iter():
        name = _local(child.tag)
        text = (child.text or "").strip()
        if name == "startDate":
            start = text[:10]
        elif name in ("endDate", "instant"):
            end = text[:10]
        elif name == "explicitMember":
            dims.append([child.get("dimension", ""), text])
        elif name == "typedMember":
            value = "".join(child.itertext()).strip()
            dims.append([child.get("dimension", ""), f"typed:{value}"])
    if end is None:
        return None
    return {"start": start, "end": end, "dims": sorted(dims)}


def _unit(node: ET.Element) -> str | None:
    if any(_local(child.tag) == "divide" for child in node):
        return None  # per-share and other ratio units are not needed
    measures = [_measure(child.text) for child in node if _local(child.tag) == "measure"]
    return measures[0] if len(measures) == 1 else None


def parse_instance(content: bytes) -> list[dict]:
    """Facts as dicts: tag, unit, val, start, end, dims. Numeric facts plus short dei text facts."""
    prefixes: dict[str, str] = {}
    root = None
    try:
        for event, item in ET.iterparse(BytesIO(content), events=("start-ns", "start")):
            if event == "start-ns":
                prefix, uri = item
                if prefix:
                    prefixes.setdefault(uri, prefix)
            elif root is None:
                root = item
    except ET.ParseError as exc:
        raise InstanceError(f"not a readable XBRL instance: {exc}") from None
    if root is None or _local(root.tag) != "xbrl":
        raise InstanceError("not an XBRL instance document")

    contexts: dict[str, dict] = {}
    units: dict[str, str | None] = {}
    for child in root:
        if _namespace(child.tag) != XBRLI:
            continue
        if _local(child.tag) == "context":
            parsed = _context(child)
            if parsed:
                contexts[child.get("id", "")] = parsed
        elif _local(child.tag) == "unit":
            units[child.get("id", "")] = _unit(child)

    facts = []
    for child in root:
        context = contexts.get(child.get("contextRef", ""))
        taxonomy = prefixes.get(_namespace(child.tag))
        if context is None or taxonomy is None:
            continue
        text = (child.text or "").strip()
        unit_ref = child.get("unitRef")
        if unit_ref is None:
            if taxonomy not in TEXT_TAXONOMIES or not text or len(text) > MAX_TEXT_LENGTH or len(child):
                continue
            facts.append({"tag": f"{taxonomy}:{_local(child.tag)}", "unit": None, "val": text, **context})
            continue
        unit = units.get(unit_ref)
        if unit is None:
            continue
        try:
            value = float(text)
        except ValueError:
            continue  # nil facts
        facts.append({"tag": f"{taxonomy}:{_local(child.tag)}", "unit": unit, "val": value, **context})
    return facts
