"""Final independent review, Hebrew catalogue (P3-1): the socket path in the
"cannot connect" text stands on its own line without bidi controls."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import re  # noqa: E402
import unittest  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class CopyablePathTests(unittest.TestCase):
    def test_cannot_connect_path_has_its_own_clean_line(self):
        sys.path.insert(0, str(REPO / "tools"))
        import po_compile
        entries = {entry.msgid: entry for entry in
                   po_compile.parse_po((REPO / "po" / "he.po").read_text(encoding="utf-8"))}
        text = entries["cannot connect to the AAG - InputLock daemon at {path} ({error}; {unit} is {state}). "
                       "Check: systemctl status {unit}"].msgstr[0]
        lines = text.split("\n")
        self.assertIn("{path}", lines)
        self.assertIn("systemctl status {unit}", lines)
        self.assertIsNone(re.search("[‎‏⁦-⁩]", lines[lines.index("{path}")]))
        self.assertNotIn("‎{path}", text)

if __name__ == "__main__":
    unittest.main()
