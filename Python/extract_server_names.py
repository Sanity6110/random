#!/usr/bin/env python3
"""
Scan *.tfvars files for server names so they can be compared against Azure.

Looks for `computer_name = "..."` assignments (the key used by the
azurerm_windows_virtual_machine "vm" blocks in this repo), wherever they are
nested inside maps/objects. If the `python-hcl2` package is installed it is
used for a fully correct parse; otherwise a dependency-free regex/brace
scanner is used as a fallback.

Usage:
    python3 extract_server_names.py [ROOT_DIR] [--key KEY ...] [--output FILE]

Examples:
    python3 extract_server_names.py
    python3 extract_server_names.py ../foundations --key computer_name --key vm_name
    python3 extract_server_names.py -o server_names.json
"""

import argparse
import json
import re
from pathlib import Path

DEFAULT_KEYS = ["computer_name"]

BLOCK_OPEN_RE = re.compile(r'^\s*"?([A-Za-z0-9_.\-]+)"?\s*=\s*\{')
BLOCK_CLOSE_RE = re.compile(r'^\s*\},?\s*$')


def find_tfvars_files(root: Path):
    return sorted(root.rglob("*.tfvars")) + sorted(root.rglob("*.tfvars.json"))


def extract_with_hcl2(path: Path, keys):
    import hcl2

    with path.open() as f:
        data = hcl2.load(f)

    results = []

    def walk(node, trail):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in keys and isinstance(v, str):
                    results.append({"path": trail, "value": v})
                walk(v, trail + [str(k)])
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, trail + [str(i)])

    walk(data, [])
    return results


def extract_with_regex(path: Path, keys):
    key_pattern = re.compile(
        r'^\s*"?(' + "|".join(re.escape(k) for k in keys) + r')"?\s*=\s*"([^"]*)"'
    )

    results = []
    stack = []
    with path.open() as f:
        for lineno, line in enumerate(f, start=1):
            m = key_pattern.match(line)
            if m:
                results.append(
                    {
                        "path": list(stack),
                        "key": m.group(1),
                        "value": m.group(2),
                        "line": lineno,
                    }
                )
                continue

            block_match = BLOCK_OPEN_RE.match(line)
            if block_match:
                stack.append(block_match.group(1))
                continue

            if BLOCK_CLOSE_RE.match(line) and stack:
                stack.pop()

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", nargs="?", default=".", help="Directory to scan (default: current directory)")
    parser.add_argument(
        "--key",
        action="append",
        dest="keys",
        help=f"tfvars key to look for (repeatable). Default: {DEFAULT_KEYS}",
    )
    parser.add_argument("-o", "--output", help="Write JSON output to this file instead of stdout")
    args = parser.parse_args()

    keys = args.keys or DEFAULT_KEYS
    root = Path(args.root).resolve()

    try:
        import hcl2  # noqa: F401

        use_hcl2 = True
    except ImportError:
        use_hcl2 = False

    tfvars_files = find_tfvars_files(root)

    all_results = []
    for path in tfvars_files:
        try:
            if use_hcl2:
                matches = extract_with_hcl2(path, keys)
            else:
                matches = extract_with_regex(path, keys)
        except Exception as exc:
            matches = []
            print(f"warning: failed to parse {path}: {exc}")

        for match in matches:
            match["file"] = str(path.relative_to(root))
            all_results.append(match)

    server_names = sorted({m["value"] for m in all_results if m.get("value")})

    output = {
        "parser": "python-hcl2" if use_hcl2 else "regex-fallback",
        "keys_searched": keys,
        "files_scanned": [str(p.relative_to(root)) for p in tfvars_files],
        "matches": all_results,
        "server_names": server_names,
    }

    text = json.dumps(output, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
        print(f"Wrote {len(all_results)} match(es) ({len(server_names)} unique server name(s)) to {args.output}")
    else:
        print(text)

    if not use_hcl2:
        print(
            "\nNote: python-hcl2 not installed, used a regex fallback. "
            "Run `pip install python-hcl2` for fully correct HCL parsing.",
        )


if __name__ == "__main__":
    main()
