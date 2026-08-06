"""Command line interface.

Exit codes:
    0  success
    1  failure (unreadable bundle, failed validation, strict flag tripped)
    2  usage error (argparse)
    3  command exists but is not implemented in this milestone
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__, layout
from .bundle import Bundle
from .errors import OxfscopeError
from .report import LEVEL_ERROR, build_summary, render, summary_json

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_NOT_IMPLEMENTED = 3

_NOT_IMPLEMENTED = {
    "collect": "M1/M2 -- probe execution is not implemented yet",
    "diff": "M4 -- run comparison is not implemented yet",
    "daemon": "M6 -- the localhost API is not implemented yet",
}


def _prog_name() -> str:
    """The CLI answers to both names; report whichever was invoked."""
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    return invoked if invoked in ("oxfscope", "performer") else "oxfscope"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog_name(),
        description=(
            "Performer collector: produce and inspect run bundles of runtime "
            "behaviour for a multi threaded Linux process."
        ),
    )
    parser.add_argument("--version", action="version", version=f"oxfscope {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    inspect = sub.add_parser(
        "inspect",
        help="print a manifest summary and quality flags for a bundle",
        description=(
            "Reads one or more run bundles (.tgz or an unpacked run directory) "
            "and prints what they contain and whether they can be trusted."
        ),
    )
    inspect.add_argument("bundle", nargs="+", type=Path)
    inspect.add_argument(
        "--json", action="store_true", help="machine readable output instead of text"
    )
    inspect.add_argument(
        "--verbose", "-v", action="store_true", help="list raw probe logs too"
    )
    inspect.add_argument(
        "--no-validate",
        action="store_true",
        help="skip schema validation (faster on large bundles)",
    )
    inspect.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when the run carries an error level quality flag",
    )
    inspect.set_defaults(func=_cmd_inspect)

    validate = sub.add_parser(
        "validate",
        help="validate a bundle against the schemas and exit non-zero on failure",
    )
    validate.add_argument("bundle", nargs="+", type=Path)
    validate.add_argument(
        "--verify-hashes",
        action="store_true",
        help="also recompute sha256 for every file listed in the manifest",
    )
    validate.add_argument("--quiet", "-q", action="store_true")
    validate.set_defaults(func=_cmd_validate)

    fake = sub.add_parser(
        "fake-run",
        aliases=["synthesize"],
        help="write a synthetic but schema valid bundle (development aid)",
        description=(
            "Generates a bundle from invented data with the shape of a real "
            "measurement. Used to develop the viewer and to prove the layout "
            "is implementable without touching a target machine."
        ),
    )
    fake.add_argument("--out", type=Path, default=Path("./runs"))
    fake.add_argument("--label", default="synthetic")
    fake.add_argument("--duration", type=int, default=60)
    fake.add_argument("--threads", type=int, default=315)
    fake.add_argument("--pid", type=int, default=205852)
    fake.add_argument("--profile", default="standard")
    fake.add_argument("--seed", type=int, default=20260806)
    fake.add_argument("--tag", action="append", default=[], dest="tags")
    fake.add_argument("--notes", default="")
    fake.add_argument(
        "--degraded",
        action="store_true",
        help="simulate a partial run: failed probe, lost events, target death",
    )
    fake.add_argument(
        "--bad-frame-pointers",
        action="store_true",
        help="simulate a target built without -fno-omit-frame-pointer",
    )
    fake.add_argument(
        "--no-pack", action="store_true", help="leave the run directory unpacked"
    )
    fake.set_defaults(func=_cmd_fake_run)

    schema = sub.add_parser("schema", help="show the bundle schemas this build enforces")
    schema.add_argument("name", nargs="?", help="schema file to print, e.g. manifest")
    schema.set_defaults(func=_cmd_schema)

    for name, reason in _NOT_IMPLEMENTED.items():
        stub = sub.add_parser(name, help=f"not implemented yet ({reason.split(' -- ')[0]})")
        stub.add_argument("args", nargs=argparse.REMAINDER)
        stub.set_defaults(func=_cmd_not_implemented, _reason=reason, _name=name)

    return parser


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def _cmd_inspect(args: argparse.Namespace) -> int:
    documents = []
    exit_code = EXIT_OK
    for index, path in enumerate(args.bundle):
        with Bundle.open(path) as bundle:
            summary = build_summary(bundle, validate=not args.no_validate)
            if args.json:
                doc = summary_json(summary)
                doc["origin"] = str(path)
                documents.append(doc)
            else:
                if index:
                    print()
                print(render(summary, origin=str(path), verbose=args.verbose))
            if summary.validation is not None and not summary.validation.ok:
                exit_code = EXIT_FAILURE
            if args.strict and summary.worst_level == LEVEL_ERROR:
                exit_code = EXIT_FAILURE
    if args.json:
        print(json.dumps(documents if len(documents) != 1 else documents[0], indent=2))
    return exit_code


def _cmd_validate(args: argparse.Namespace) -> int:
    exit_code = EXIT_OK
    for path in args.bundle:
        with Bundle.open(path) as bundle:
            report = bundle.validate(verify_hashes=args.verify_hashes)
            if report.ok:
                if not args.quiet:
                    print(f"OK    {path}  ({len(report.checked)} file(s) validated)")
            else:
                exit_code = EXIT_FAILURE
                print(f"FAIL  {path}")
                for problem in report.flat():
                    print(f"        {problem}")
    return exit_code


def _cmd_fake_run(args: argparse.Namespace) -> int:
    from . import fake

    run_dir, archive = fake.generate(
        args.out,
        label=args.label,
        duration_s=args.duration,
        thread_count=args.threads,
        pid=args.pid,
        profile=args.profile,
        seed=args.seed,
        degraded=args.degraded,
        bad_frame_pointers=args.bad_frame_pointers,
        tags=args.tags or ["synthetic"],
        notes=args.notes,
        pack=not args.no_pack,
    )
    print(f"run directory: {run_dir}")
    if archive != run_dir:
        print(f"bundle:        {archive}")
    print(f"inspect with:  {_prog_name()} inspect {archive}")
    return EXIT_OK


def _cmd_schema(args: argparse.Namespace) -> int:
    directory = layout.schema_dir()
    if args.name:
        name = args.name if args.name.endswith(".json") else f"{args.name}.schema.json"
        path = directory / name
        if not path.is_file():
            raise OxfscopeError(f"no such schema: {path}")
        sys.stdout.write(path.read_text(encoding="utf-8"))
        return EXIT_OK
    print(f"schema directory: {directory}")
    for path in sorted(directory.glob("*.schema.json")):
        print(f"  {path.name}")
    print()
    print(f"bundle schema_version: {layout.SCHEMA_VERSION}")
    return EXIT_OK


def _cmd_not_implemented(args: argparse.Namespace) -> int:
    print(
        f"{_prog_name()} {args._name}: not implemented yet ({args._reason}).",
        file=sys.stderr,
    )
    print(
        "This milestone (M0) fixes the bundle format only. "
        "See README.md for the milestone plan.",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


# --------------------------------------------------------------------------


def _stub_command(argv: Sequence[str]) -> Optional[str]:
    """Detect a not-yet-implemented command before argparse sees its flags.

    ``collect --pid 205852 ...`` must answer "not implemented yet", not
    "unrecognized arguments: --pid": the operator's mistake is the milestone,
    not the syntax.  ``--help`` still falls through to argparse.
    """
    if any(flag in argv for flag in ("-h", "--help")):
        return None
    for token in argv:
        if not token.startswith("-"):
            return token if token in _NOT_IMPLEMENTED else None
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    stub = _stub_command(raw)
    if stub is not None:
        namespace = argparse.Namespace(_name=stub, _reason=_NOT_IMPLEMENTED[stub])
        return _cmd_not_implemented(namespace)

    parser = _build_parser()
    args = parser.parse_args(raw)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return EXIT_OK
    try:
        return args.func(args)
    except OxfscopeError as exc:
        print(f"oxfscope: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except BrokenPipeError:  # pragma: no cover - piping into head
        return EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
