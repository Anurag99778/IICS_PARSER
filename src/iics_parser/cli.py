"""Command line interface.

    iics-parser export.zip                      one package
    iics-parser exports/ -o out/                every zip in a folder
    iics-parser export.zip --ai                 draft narrative fields too
    iics-parser export.zip --init-overrides o.yaml   scaffold the manual fields
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

from .enrich import ai as ai_enrich
from .enrich.overrides import load_overrides, write_template
from .pipeline import Result, process


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="iics-parser",
        description="Turn Informatica IICS export packages into migration "
                    "analysis documents (Excel + Word).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("input", type=Path,
                   help="An export .zip, or a folder containing export zips.")
    p.add_argument("-o", "--out", type=Path, default=Path("output"),
                   help="Output directory (default: ./output).")
    p.add_argument("--overrides", type=Path,
                   help="YAML file of values the export package cannot supply.")
    p.add_argument("--init-overrides", type=Path, metavar="FILE",
                   help="Write a starter overrides file for the input and exit.")
    p.add_argument("--ai", action="store_true",
                   help="Draft narrative fields with GPT (needs OPENAI_API_KEY).")
    p.add_argument("--template", type=Path,
                   help="Word template to inherit styles from.")
    p.add_argument("--format", choices=["all", "excel", "word"], default="all",
                   help="Which documents to produce (default: all).")
    p.add_argument("--dump-ir", action="store_true",
                   help="Also write the parsed intermediate representation as JSON.")
    p.add_argument("-q", "--quiet", action="store_true", help="Only report problems.")
    return p


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    zips = _collect(args.input)
    if not zips:
        print(f"error: no export zips found at {args.input}", file=sys.stderr)
        return 2

    if args.ai and not ai_enrich.is_available():
        print("warning: --ai requested but no OPENAI_API_KEY or openai SDK found; "
              "narrative fields will be left for a human.", file=sys.stderr)

    overrides = load_overrides(args.overrides)
    if args.overrides and not Path(args.overrides).exists():
        print(f"warning: overrides file {args.overrides} not found; ignoring.",
              file=sys.stderr)

    results: List[Result] = []
    for zip_path in zips:
        if not args.quiet:
            print(f"→ {zip_path.name}")
        result = process(
            zip_path, args.out,
            overrides=overrides, use_ai=args.ai, template=args.template,
            formats=args.format, dump_ir=args.dump_ir,
        )
        results.append(result)

        if args.init_overrides:
            if result.integration:
                path = write_template(result.integration, args.init_overrides)
                print(f"  wrote overrides template: {path}")
            continue

        _report(result, quiet=args.quiet)

    return _summary(results, quiet=args.quiet)


def _collect(path: Path) -> List[Path]:
    path = Path(path)
    if path.is_dir():
        return sorted(path.glob("*.zip"))
    return [path] if path.is_file() else []


def _report(result: Result, quiet: bool) -> None:
    if not result.ok:
        print(f"  FAILED: {result.error}", file=sys.stderr)
        return

    integration = result.integration
    if not quiet and integration:
        print(f"  {len(integration.steps)} steps, "
              f"{len(integration.mappings)} mappings, "
              f"{len(integration.connections)} connections")
        for out in result.outputs:
            print(f"  wrote {out}")

    if integration and integration.warnings:
        print(f"  {len(integration.warnings)} item(s) need review "
              f"(see the Parse Report sheet):")
        for warning in integration.warnings[:5]:
            print(f"    - {warning}")
        if len(integration.warnings) > 5:
            print(f"    ... and {len(integration.warnings) - 5} more")


def _summary(results: List[Result], quiet: bool) -> int:
    failed = [r for r in results if not r.ok]
    if len(results) > 1 or failed:
        print(f"\n{len(results) - len(failed)}/{len(results)} package(s) processed.")
    for r in failed:
        print(f"  failed: {r.zip_path.name} - {r.error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
