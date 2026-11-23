"""deltawatch-dist end to end: one command builds everything; failures give a non-zero exit."""

from __future__ import annotations

import json
from pathlib import Path

from deltawatch.distribution.cli import main

REPO = Path(__file__).resolve().parents[1]


def test_build_verify_and_refusals(tmp_path):
    cat, rep = tmp_path / "catalog", tmp_path / "reports"
    args = ["--repo-root", str(REPO), "--catalog-dir", str(cat), "--report-dir", str(rep)]
    assert main(["build", "--config", str(REPO / "config/distribution.yaml"), *args]) == 0
    cog = json.loads((rep / "cog_build_report.json").read_text())
    assert cog["all_valid"] and cog["all_identical"] and cog["all_pairs_consistent"] and len(cog["files"]) == 16
    stac = json.loads((rep / "catalog_validation_report.json").read_text())
    assert stac["broken_links"] == [] and stac["asset_integrity"]["ok"] and stac["items"] == 8

    assert main(["verify", "--catalog-dir", str(cat)]) == 0
    tif = cat / "deltawatch-ndvi" / "2025-04-17" / "ndvi_2025-04-17.tif"
    tif.write_bytes(tif.read_bytes() + b"\x00")
    assert main(["verify", "--catalog-dir", str(cat)]) == 1

    # A rebuild only clears a folder under <repo>/build; anything else is left alone.
    assert main(["build", "--config", str(REPO / "config/distribution.yaml"), *args]) == 1
    assert tif.exists()


def test_unknown_date_is_an_error_exit(tmp_path):
    argv = ["cog", "--config", str(REPO / "config/distribution.yaml"), "--repo-root", str(REPO)]
    argv += ["--catalog-dir", str(tmp_path), "--report-dir", str(tmp_path), "--dates", "2025-01-01"]
    assert main(argv) == 1


def test_workflows_keep_reports_outside_the_catalog():
    """The STAC validator reads every JSON under the catalog, so no workflow may put reports there."""
    import re

    from deltawatch.distribution.cli import DEFAULT_CATALOG

    for name in ("pages.yml", "ci.yml"):
        text = (REPO / ".github" / "workflows" / name).read_text()
        cmds = re.findall(r"deltawatch-dist build(.*)", text)
        assert cmds, name
        for tail in cmds:
            m = re.search(r"--report-dir\s+(\S+)", tail)
            assert m, name
            report = Path(m.group(1)).resolve()
            catalog = Path(
                re.search(r"--catalog-dir\s+(\S+)", tail).group(1) if "--catalog-dir" in tail else DEFAULT_CATALOG
            ).resolve()
            assert catalog not in (report, *report.parents), f"{name}: reports inside the catalog"
