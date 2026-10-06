# test.py
"""
A simple utility script that demonstrates a basic entry point,
argument parsing, and a reusable function.

Run with:
    python test.py          # prints a greeting
    python test.py --name Alice  # prints a personalized greeting
"""

import argparse
import sys


def greet(name: str) -> str:
    """Return a greeting message for the given name."""
    return f"Hello, {name}!"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command‑line arguments."""
    parser = argparse.ArgumentParser(description="Demo greeting script.")
    parser.add_argument(
        "--name",
        type=str,
        default="World",
        help="Name to greet (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main() -> int:
    """Program entry point."""
    args = parse_args()
    message = greet(args.name)
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
