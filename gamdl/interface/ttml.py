from html import escape as xml_escape
from xml.dom import minidom


def serialize_ttml_pretty(lyrics_ttml: str) -> str:
    document = minidom.parseString(lyrics_ttml)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + _serialize_ttml_node(document.documentElement)
    )


def _serialize_ttml_node(
    node: minidom.Node,
    indent: int = 0,
    indent_str: str = "  ",
) -> str:
    if node.nodeType == minidom.Node.TEXT_NODE:
        return _escape_ttml_text(node.data)

    if node.nodeType == minidom.Node.ELEMENT_NODE:
        attrs = _get_ttml_attrs(node)
        children = _get_ttml_children(node)

        if not children:
            return f"<{node.tagName}{attrs}/>"

        if all(child.nodeType == minidom.Node.TEXT_NODE for child in children):
            text = _serialize_ttml_text_content(children)
            return f"<{node.tagName}{attrs}>{text}</{node.tagName}>"

        child_elements = [
            child
            for child in children
            if child.nodeType == minidom.Node.ELEMENT_NODE
        ]
        if child_elements and all(
            child.tagName == "span" for child in child_elements
        ):
            return _serialize_ttml_inline(node)

        lines = [f"<{node.tagName}{attrs}>"]
        for child in children:
            if child.nodeType == minidom.Node.ELEMENT_NODE:
                lines.append(
                    indent_str * (indent + 1)
                    + _serialize_ttml_node(child, indent + 1, indent_str)
                )
            elif child.data.strip():
                lines.append(
                    indent_str * (indent + 1) + _escape_ttml_text(child.data.strip())
                )
        lines.append(indent_str * indent + f"</{node.tagName}>")
        return "\n".join(lines)

    return ""


def _serialize_ttml_inline(node: minidom.Node) -> str:
    attrs = _get_ttml_attrs(node)
    children = _get_ttml_children(node)

    if not children:
        return f"<{node.tagName}{attrs}/>"

    if all(child.nodeType == minidom.Node.TEXT_NODE for child in children):
        text = _serialize_ttml_text_content(children)
        return f"<{node.tagName}{attrs}>{text}</{node.tagName}>"

    parts = []
    for child in children:
        if child.nodeType == minidom.Node.ELEMENT_NODE:
            parts.append(_serialize_ttml_inline(child))
        elif child.data:
            parts.append(_escape_ttml_separator(child.data))
    return f"<{node.tagName}{attrs}>{''.join(parts)}</{node.tagName}>"


def _serialize_ttml_text_content(children: list[minidom.Node]) -> str:
    return _escape_ttml_text("".join(child.data for child in children))


def _get_ttml_attrs(node: minidom.Node) -> str:
    return "".join(
        f' {name}="{_escape_ttml_attr(value)}"'
        for name, value in node.attributes.items()
    )


def _get_ttml_children(node: minidom.Node) -> list[minidom.Node]:
    return [
        child
        for child in node.childNodes
        if child.nodeType in (minidom.Node.ELEMENT_NODE, minidom.Node.TEXT_NODE)
    ]


def _escape_ttml_text(value: str) -> str:
    return xml_escape(value, quote=False).replace("\xa0", "&#x00A0;")


def _escape_ttml_attr(value: str) -> str:
    return xml_escape(value, quote=True).replace("\xa0", "&#x00A0;")


def _escape_ttml_separator(value: str) -> str:
    separator = "".join(char for char in value if char not in "\r\n\t")
    return _escape_ttml_text(separator)
