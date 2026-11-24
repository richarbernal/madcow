"""Ejecuta los tests de JavaScript (indicadores) si Node.js está instalado."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js no está instalado")
def test_indicators_js():
    r = subprocess.run(["node", "--test", "tests/js/indicators.test.mjs"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
