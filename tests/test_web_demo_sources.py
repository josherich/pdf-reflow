"""Guards the module list the WASM demo copies into Pyodide's filesystem.

``web/app.js`` cannot discover ``src/pdf_reflow/*.py`` at runtime (GitHub
Pages serves no directory listing), so it hard-codes the filenames. Forgetting
to add a newly created module there only shows up in the browser as
``ModuleNotFoundError``, which is why this test compares the list against the
package directory.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "app.js"
PACKAGE = ROOT / "src" / "pdf_reflow"


def web_demo_sources() -> list[str]:
    """Return the quoted filenames from PDF_REFLOW_SOURCES in web/app.js."""
    text = APP_JS.read_text(encoding="utf-8")
    match = re.search(
        r"const PDF_REFLOW_SOURCES = \[(.*?)\];", text, re.DOTALL
    )
    assert match, "PDF_REFLOW_SOURCES not found in web/app.js"
    return re.findall(r'"([^"]+)"', match.group(1))


class WebDemoSourcesTest(unittest.TestCase):
    def test_list_matches_package_directory(self):
        expected = {p.name for p in PACKAGE.glob("*.py")}
        self.assertEqual(
            sorted(web_demo_sources()),
            sorted(expected),
            "web/app.js PDF_REFLOW_SOURCES is out of sync with "
            "src/pdf_reflow/*.py; the WASM demo would raise "
            "ModuleNotFoundError in the browser.",
        )


if __name__ == "__main__":
    unittest.main()
