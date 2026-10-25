"""Validate and compile the application catalog without importing GUI code."""
import ast
from pathlib import Path
import string
import subprocess
import sys
import xml.etree.ElementTree as ET


def catalog_sources(root):
    sources = set()
    package = Path(root) / "src/modbus_dev"
    for path in [*package.joinpath("ui").rglob("*.py"), package / "desktop.py"]:
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                sources.add(node.args[0].value)
    return sources


def catalog_path(root):
    return Path(root) / "src/modbus_dev/ui/translations/modbus_dev_zh_CN.ts"


def placeholders(text):
    return sorted(field for _, field, _, _ in string.Formatter().parse(text) if field is not None)


def validate_catalog(root):
    catalog = ET.parse(catalog_path(root))
    messages = {}
    for context in catalog.getroot().findall("context"):
        if context.findtext("name") != "ModbusDev":
            continue
        for message in context.findall("message"):
            source = message.findtext("source", "")
            translation = message.find("translation")
            if source in messages:
                raise ValueError(f"Duplicate translation source: {source}")
            if translation is None or translation.get("type") == "unfinished" or not translation.text:
                raise ValueError(f"Unfinished translation: {source}")
            if placeholders(source) != placeholders(translation.text):
                raise ValueError(f"Translation placeholder mismatch: {source}")
            messages[source] = translation.text
    missing = catalog_sources(root) - messages.keys()
    if missing:
        raise ValueError("Missing translations: " + "; ".join(sorted(missing)))
    return len(messages)


def update_catalog(root):
    path = catalog_path(root)
    tree = ET.parse(path)
    context = next(context for context in tree.getroot().findall("context") if context.findtext("name") == "ModbusDev")
    existing = {message.findtext("source") for message in context.findall("message")}
    for source in sorted(catalog_sources(root) - existing):
        message = ET.SubElement(context, "message")
        ET.SubElement(message, "source").text = source
        ET.SubElement(message, "translation", type="unfinished")
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def compile_catalog(root):
    count = validate_catalog(root)
    source = catalog_path(root)
    destination = source.with_suffix(".qm")
    subprocess.run([sys.executable, "-c", "from PySide6.scripts.pyside_tool import lrelease; lrelease()",
                    str(source), "-qm", str(destination)], check=True)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"Translation compilation produced no catalog: {destination}")
    return count
