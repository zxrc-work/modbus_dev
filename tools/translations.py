"""Maintain Qt source-text catalogs: --update, --check or --compile."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from release_tools.translations import compile_catalog, update_catalog, validate_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--update", action="store_true", help="add newly marked source text as unfinished entries")
    actions.add_argument("--compile", action="store_true", help="check completeness and compile the QM catalog")
    actions.add_argument("--check", action="store_true", help="check completeness and format placeholders")
    args = parser.parse_args()
    if args.update:
        update_catalog(ROOT)
        print("Updated catalog sources; complete unfinished translations before compiling.")
    else:
        count = compile_catalog(ROOT) if args.compile else validate_catalog(ROOT)
        print(f"Validated {count} translation messages.")


if __name__ == "__main__":
    main()
