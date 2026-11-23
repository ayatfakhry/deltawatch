"""deltawatch-dist: package frozen DeltaWatch outputs as COGs + a static STAC catalog.

  build          COGs + catalog + all reports in one command (what CI and the Pages workflow run)
  cog            COGs only, with validation and equality reports
  catalog        STAC catalog from existing COGs, then schema validation + link walk
  verify         recompute every asset checksum/size against the catalog
  serve          minimal local query service (not a STAC API), 127.0.0.1 only by default
  check-hosting  Range/CORS check against a deployed host; writes the exact request/response

Every command exits non-zero on any failure. The NDVI pipeline is never run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .cog import DistributionError, build_dates, load_distribution_config

log = logging.getLogger("deltawatch.distribution")
DEFAULT_CATALOG = "build/distribution/catalog"


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def _cog(args: argparse.Namespace) -> int:
    from .stac import COLLECTION_ID

    cfg = load_distribution_config(args.config)
    report = build_dates(cfg, args.repo_root, Path(args.catalog_dir) / COLLECTION_ID, args.dates or None)
    rep = Path(args.report_dir)
    _write(rep / "cog_build_report.json", {"config": str(args.config), **report})
    _write(
        rep / "cog_validation_report.json",
        {"tool_versions": report["tool_versions"], "validation": report["validation"]},
    )
    _write(
        rep / "cog_equality_report.json",
        {
            "tool_versions": report["tool_versions"],
            "equality": report["equality"],
            "pair_semantics": report["pair_semantics"],
        },
    )
    log.info(
        "%d COGs: all_valid=%s all_identical=%s pairs_consistent=%s",
        len(report["files"]),
        report["all_valid"],
        report["all_identical"],
        report["all_pairs_consistent"],
    )
    return 0


def _catalog(args: argparse.Namespace) -> int:
    import pystac

    from .stac import build_catalog, validate_catalog, verify_assets

    cfg = load_distribution_config(args.config)
    build_catalog(cfg, args.repo_root, args.catalog_dir)
    result = {
        "pystac_version": pystac.__version__,
        "stac_version": pystac.get_stac_version(),
        **validate_catalog(args.catalog_dir),
        "asset_integrity": verify_assets(args.catalog_dir),
    }
    _write(Path(args.report_dir) / "catalog_validation_report.json", result)
    if result["broken_links"] or not result["asset_integrity"]["ok"]:
        log.error("catalog check failed: %s", json.dumps(result, indent=1))
        return 1
    log.info("catalog valid: %d documents, %d links, 0 broken", result["documents_validated"], result["links_checked"])
    return 0


def _build(args: argparse.Namespace) -> int:
    import shutil

    out = Path(args.catalog_dir).resolve()
    build_root = (Path(args.repo_root) / "build").resolve()
    if out.exists():
        if not out.is_relative_to(build_root):
            log.error("%s exists and is outside %s; refusing to clear it", out, build_root)
            return 1
        shutil.rmtree(out)  # a rebuild never mixes old and new files
    args.dates = None
    return _cog(args) or _catalog(args)


def _verify(args: argparse.Namespace) -> int:
    from .stac import verify_assets

    r = verify_assets(args.catalog_dir)
    print(json.dumps(r, indent=2))
    return 0 if r["ok"] else 1


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .query import create_app

    uvicorn.run(create_app(args.catalog_dir), host=args.host, port=args.port)
    return 0


def _check_hosting(args: argparse.Namespace) -> int:
    from .hosting import check_host

    r = check_host(args.cog_url, args.catalog_url, args.origin)
    _write(Path(args.out), r)
    print(json.dumps(r, indent=2))
    return 0 if r["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(
        prog="deltawatch-dist", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, report=True):
        sp.add_argument("--config", default="config/distribution.yaml")
        sp.add_argument("--repo-root", default=".")
        sp.add_argument("--catalog-dir", default=DEFAULT_CATALOG)
        if report:
            sp.add_argument("--report-dir", default="reports/distribution")

    common(b := sub.add_parser("build", help="COGs + catalog + reports"))
    b.set_defaults(func=_build)
    common(c := sub.add_parser("cog", help="COGs with validation/equality reports"))
    c.add_argument("--dates", nargs="*", help="subset of frozen dates (default: all)")
    c.set_defaults(func=_cog)
    common(k := sub.add_parser("catalog", help="STAC catalog from existing COGs + validation"))
    k.set_defaults(func=_catalog)
    v = sub.add_parser("verify", help="recompute asset checksums against the catalog")
    v.add_argument("--catalog-dir", default=DEFAULT_CATALOG)
    v.set_defaults(func=_verify)
    s = sub.add_parser("serve", help="local query service (not a STAC API)")
    s.add_argument("--catalog-dir", default=DEFAULT_CATALOG)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=_serve)
    h = sub.add_parser("check-hosting", help="Range/CORS check against a deployed host")
    h.add_argument("--cog-url", required=True)
    h.add_argument("--catalog-url", required=True)
    h.add_argument("--origin", default="https://radiantearth.github.io", help="browser origin to test (STAC Browser)")
    h.add_argument("--out", default="reports/distribution/hosting_check.json")
    h.set_defaults(func=_check_hosting)

    args = p.parse_args(argv)
    import pystac

    try:
        return args.func(args)
    except (DistributionError, pystac.STACValidationError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
