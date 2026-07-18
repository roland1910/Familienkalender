"""Guards on the add-on start script (run.sh).

The production container is the ONLY place where some environment
contracts must hold; unit tests cannot execute run.sh, so these tests
pin the contract textually (same approach as test_frontend_static.py).
"""

from pathlib import Path

RUN_SH = Path(__file__).resolve().parent.parent / "run.sh"


class TestRunShContracts:
    def test_exports_slideshow_scan_flag(self):
        """The hourly photo rescan (app/main.py: periodic_photo_scan) only
        runs with SLIDESHOW_SCAN=1 — production must set it, otherwise new
        photos never enter the slideshow index without a manual rescan."""
        content = RUN_SH.read_text(encoding="utf-8")
        assert "export SLIDESHOW_SCAN=1" in content
