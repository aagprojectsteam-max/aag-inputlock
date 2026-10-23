#!/usr/bin/env python3
"""Tiny pure-Python .po -> .mo compiler (GNU gettext format, stdlib only).

install.sh uses ``msgfmt`` when the host has it and this script otherwise.
Like ``msgfmt`` without ``--use-fuzzy``, fuzzy and untranslated entries are
left out, so they fall back to English at run time.

    python3 tools/po_compile.py po/he.po out.mo     compile one catalogue
    python3 tools/po_compile.py --build             po/*.po -> po/build/<lang>/LC_MESSAGES/input-lock.mo

The second form prepares a source checkout: input_lock_i18n uses po/build
when it runs from src/.
"""

from __future__ import annotations

import argparse
import array
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

DOMAIN = "input-lock"
ROOT = Path(__file__).resolve().parents[1]
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "a": "\a",
            "b": "\b", "f": "\f", "v": "\v"}


class PoError(ValueError):
    pass


@dataclass
class Entry:
    msgid: str = ""
    msgid_plural: str | None = None
    msgctxt: str | None = None
    msgstr: dict[int, str] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    references: list[str] = field(default_factory=list)
    obsolete: bool = False
    line: int = 0

    @property
    def fuzzy(self) -> bool:
        return "fuzzy" in self.flags

    @property
    def translated(self) -> bool:
        return bool(self.msgstr) and all(self.msgstr.values())


def _unquote(text: str, path: str, line: int) -> str:
    text = text.strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        raise PoError(f"{path}:{line}: expected a quoted string")
    body, out, index = text[1:-1], [], 0
    while index < len(body):
        char = body[index]
        if char == '"':
            raise PoError(f"{path}:{line}: unescaped quote")
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(body) or body[index + 1] not in _ESCAPES:
            raise PoError(f"{path}:{line}: unsupported escape sequence")
        out.append(_ESCAPES[body[index + 1]])
        index += 2
    return "".join(out)


def parse_po(text: str, path: str = "<po>") -> list[Entry]:
    """Parse PO text into entries (the header is the entry with msgid "")."""
    entries: list[Entry] = []
    entry = Entry()
    keyword: str | None = None
    seen_keyword = False

    def finish() -> None:
        nonlocal entry, keyword, seen_keyword
        if seen_keyword:
            if not entry.msgstr:
                raise PoError(f"{path}:{entry.line}: entry without msgstr")
            entries.append(entry)
        entry, keyword, seen_keyword = Entry(), None, False

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        obsolete = line.startswith("#~")
        if obsolete:
            line = line[2:].strip()
        if not line:
            finish()
            continue
        if line.startswith("#"):
            if seen_keyword and keyword in {"msgstr"} | {f"msgstr[{n}]" for n in range(10)}:
                finish()
            if line.startswith("#,"):
                entry.flags.update(flag.strip() for flag in line[2:].split(",") if flag.strip())
            elif line.startswith("#:"):
                entry.references.extend(line[2:].split())
            continue
        if line.startswith('"'):
            if keyword is None:
                raise PoError(f"{path}:{number}: continuation without a keyword")
            value = _unquote(line, path, number)
        else:
            name, _space, rest = line.partition(" ")
            if name == "msgctxt" or (name == "msgid" and keyword is not None and keyword.startswith("msgstr")):
                finish()
            if not seen_keyword:
                entry.line = number
            seen_keyword = True
            entry.obsolete = entry.obsolete or obsolete
            keyword = name
            value = _unquote(rest, path, number)
            if name == "msgctxt":
                entry.msgctxt = ""
            elif name == "msgid":
                entry.msgid = ""
            elif name == "msgid_plural":
                entry.msgid_plural = ""
            elif name == "msgstr":
                entry.msgstr[0] = ""
            elif name.startswith("msgstr[") and name.endswith("]") and name[7:-1].isdigit():
                entry.msgstr[int(name[7:-1])] = ""
            else:
                raise PoError(f"{path}:{number}: unknown keyword {name}")
        if keyword == "msgctxt":
            entry.msgctxt = (entry.msgctxt or "") + value
        elif keyword == "msgid":
            entry.msgid += value
        elif keyword == "msgid_plural":
            entry.msgid_plural = (entry.msgid_plural or "") + value
        elif keyword == "msgstr":
            entry.msgstr[0] += value
        else:
            index = int(keyword[7:-1])
            entry.msgstr[index] += value
    finish()
    keys = [(item.msgctxt, item.msgid) for item in entries if not item.obsolete]
    if len(keys) != len(set(keys)):
        raise PoError(f"{path}: duplicate msgid")
    return entries


def compile_entries(entries: list[Entry]) -> bytes:
    """GNU .mo bytes for the translated, non-fuzzy entries (header included)."""
    catalog: dict[bytes, bytes] = {}
    for entry in entries:
        if entry.obsolete:
            continue
        is_header = entry.msgid == "" and entry.msgctxt is None
        if not is_header and (entry.fuzzy or not entry.translated):
            continue
        key = entry.msgid
        if entry.msgid_plural is not None:
            key += "\0" + entry.msgid_plural
        if entry.msgctxt is not None:
            key = entry.msgctxt + "\x04" + key
        values = [entry.msgstr[index] for index in sorted(entry.msgstr)]
        catalog[key.encode("utf-8")] = "\0".join(values).encode("utf-8")
    keys = sorted(catalog)
    ids = b"".join(key + b"\0" for key in keys)
    strs = b"".join(catalog[key] + b"\0" for key in keys)
    count = len(keys)
    key_start = 7 * 4 + 16 * count
    value_start = key_start + len(ids)
    offsets: list[int] = []
    position = 0
    for key in keys:
        offsets += [len(key), key_start + position]
        position += len(key) + 1
    position = 0
    for key in keys:
        offsets += [len(catalog[key]), value_start + position]
        position += len(catalog[key]) + 1
    output = struct.pack("Iiiiiii", 0x950412DE, 0, count, 7 * 4, 7 * 4 + count * 8, 0, 0)
    table = array.array("i", offsets)
    return output + table.tobytes() + ids + strs


def compile_file(source: Path, target: Path) -> int:
    """Compile ``source`` to ``target``; return the number of translated messages."""
    entries = parse_po(source.read_text(encoding="utf-8"), str(source))
    data = compile_entries(entries)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return sum(1 for item in entries if item.msgid and not item.obsolete
               and not item.fuzzy and item.translated)


def build(po_dir: Path, out_dir: Path) -> list[Path]:
    built = []
    for source in sorted(po_dir.glob("*.po")):
        target = out_dir / source.stem / "LC_MESSAGES" / f"{DOMAIN}.mo"
        compile_file(source, target)
        built.append(target)
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile gettext .po catalogues to .mo.")
    parser.add_argument("--build", action="store_true",
                        help="compile po/*.po into po/build/<lang>/LC_MESSAGES/")
    parser.add_argument("source", nargs="?", type=Path)
    parser.add_argument("target", nargs="?", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.build:
            for path in build(ROOT / "po", ROOT / "po" / "build"):
                print(path.relative_to(ROOT))
            return 0
        if args.source is None or args.target is None:
            parser.error("give SOURCE.po and TARGET.mo, or --build")
        compile_file(args.source, args.target)
        return 0
    except (OSError, PoError) as exc:
        print(f"po_compile: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
