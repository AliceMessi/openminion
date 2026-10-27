import sys
import os
from pathlib import Path

import pytest

from openminion.tools.security.process import run_scanner


def test_scanner_process_timeout_and_cancellation(tmp_path: Path) -> None:
    timed_out = run_scanner(
        (sys.executable, "-c", "import time; time.sleep(2)"),
        cwd=tmp_path,
        timeout_seconds=0.01,
    )
    cancelled = run_scanner(
        (sys.executable, "-c", "raise SystemExit(130)"),
        cwd=tmp_path,
        timeout_seconds=2,
    )
    assert timed_out.timed_out is True
    assert timed_out.return_code == 124
    assert cancelled.cancelled is True
    assert cancelled.return_code == 130


@pytest.mark.skipif(os.name != "posix", reason="POSIX scanner process groups")
def test_scanner_timeout_terminates_descendant_holding_output(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-finished"
    child = (
        "import pathlib,time; time.sleep(3); "
        f"pathlib.Path({str(marker)!r}).write_text('still running')"
    )
    command = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "print('child started', flush=True); time.sleep(5)"
    )
    result = run_scanner(
        (sys.executable, "-c", command), cwd=tmp_path, timeout_seconds=1
    )
    assert result.timed_out and result.return_code == 124
    assert "child started" in result.stdout
    assert not marker.exists(), "scanner descendant survived the timeout"
