#!/usr/bin/env python3
"""Extract AAG - InputLock's translatable strings into po/input-lock.pot.

Stdlib only (``ast``), so contributors do not need xgettext. Recognised
markers, with constant string arguments only:

    _("text")  N_("text")  ngettext("one", "many", n)  pgettext("context", "text")

plus ``_t 'text'`` in the shell launcher. A call with a non-constant argument
(``_(LABEL)``) is a run-time lookup of a string marked elsewhere with N_().

    python3 tools/po_extract.py           rewrite po/input-lock.pot
    python3 tools/po_extract.py --check   fail if the .pot is out of date
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POT = ROOT / "po" / "input-lock.pot"
PYTHON_SOURCES = ("src/*.py",)
SHELL_SOURCES = ("src/input-lock-launch",)
SHELL_MARKER = re.compile(r"_t '([^']*)'")
BRACE_FIELD = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
KEYWORDS = {
    "_": ("msgid",),
    "N_": ("msgid",),
    "ngettext": ("msgid", "plural"),
    "pgettext": ("context", "msgid"),
}


@dataclass
class Message:
    msgid: str
    plural: str | None = None
    context: str | None = None
    references: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str | None, str]:
        return (self.context, self.msgid)

    @property
    def brace_format(self) -> bool:
        return bool(BRACE_FIELD.search(self.msgid + (self.plural or "")))


def _marker_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in KEYWORDS:
        return func.id
    if (isinstance(func, ast.Attribute) and func.attr in KEYWORDS
            and isinstance(func.value, ast.Name) and func.value.id == "i18n"):
        return func.attr
    return None


def _python_messages(path: Path, relative: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _marker_name(node)
        if name is None:
            continue
        roles = KEYWORDS[name]
        if len(node.args) < len(roles):
            continue
        values = {}
        for role, argument in zip(roles, node.args):
            if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
                break
            values[role] = argument.value
        else:
            yield Message(values["msgid"], values.get("plural"), values.get("context"),
                          [f"{relative}:{node.lineno}"])


def _shell_messages(path: Path, relative: str):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in SHELL_MARKER.finditer(line):
            yield Message(match.group(1), references=[f"{relative}:{number}"])


def extract(root: Path = ROOT) -> dict[tuple[str | None, str], Message]:
    """Every translatable message in the sources, keyed by (context, msgid)."""
    found: dict[tuple[str | None, str], Message] = {}
    files: list[tuple[Path, str]] = []
    for pattern in PYTHON_SOURCES:
        files += [(path, "python") for path in sorted(root.glob(pattern))]
    for pattern in SHELL_SOURCES:
        files += [(path, "shell") for path in sorted(root.glob(pattern))]
    for path, kind in files:
        relative = path.relative_to(root).as_posix()
        reader = _python_messages if kind == "python" else _shell_messages
        for message in sorted(reader(path, relative), key=lambda m: int(m.references[0].rsplit(":", 1)[1])):
            if not message.msgid:
                raise ValueError(f"{message.references[0]}: empty msgid")
            known = found.get(message.key)
            if known is None:
                found[message.key] = message
                continue
            if known.plural != message.plural:
                raise ValueError(f"{message.references[0]}: {message.msgid!r} is used both with "
                                 "and without a plural form")
            known.references += message.references
    return found


def _quote(text: str) -> str:
    return '"' + (text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
                  .replace("\n", "\\n")) + '"'


def po_string(keyword: str, text: str) -> list[str]:
    """``keyword "text"``; a text with newlines is split after each one."""
    if "\n" not in text[:-1]:
        return [f"{keyword} {_quote(text)}"]
    lines = [f'{keyword} ""']
    pieces = text.split("\n")
    for index, piece in enumerate(pieces):
        last = index == len(pieces) - 1
        if last and not piece:
            break
        lines.append(_quote(piece + ("" if last else "\n")))
    return lines


def render_pot(messages: dict[tuple[str | None, str], Message], version: str) -> str:
    lines = [
        "# AAG - InputLock user interface.",
        "# This file is distributed under the same license as the AAG - InputLock package.",
        "# Regenerate with: python3 tools/po_extract.py",
        "#",
        "#, fuzzy",
        'msgid ""',
        'msgstr ""',
        f'"Project-Id-Version: input-lock {version}\\n"',
        '"Report-Msgid-Bugs-To: AAG.Projects.Team@gmail.com\\n"',
        '"Language: \\n"',
        '"MIME-Version: 1.0\\n"',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        '"Content-Transfer-Encoding: 8bit\\n"',
        '"Plural-Forms: nplurals=INTEGER; plural=EXPRESSION;\\n"',
        "",
    ]
    for message in messages.values():
        lines.append("#: " + " ".join(message.references))
        if message.brace_format:
            lines.append("#, python-brace-format")
        if message.context is not None:
            lines += po_string("msgctxt", message.context)
        lines += po_string("msgid", message.msgid)
        if message.plural is not None:
            lines += po_string("msgid_plural", message.plural)
            lines += ['msgstr[0] ""', 'msgstr[1] ""']
        else:
            lines.append('msgstr ""')
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update po/input-lock.pot from the sources.")
    parser.add_argument("--check", action="store_true", help="only check that the .pot is current")
    args = parser.parse_args(argv)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    text = render_pot(extract(ROOT), version)
    if args.check:
        current = POT.read_text(encoding="utf-8") if POT.exists() else ""
        if current != text:
            print("po/input-lock.pot is out of date; run: python3 tools/po_extract.py", file=sys.stderr)
            return 1
        return 0
    POT.parent.mkdir(exist_ok=True)
    POT.write_text(text, encoding="utf-8")
    print(f"{POT.relative_to(ROOT)}: {len(extract(ROOT))} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
