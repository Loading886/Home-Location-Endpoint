#!/usr/bin/env python3
"""Render the deterministic iOS CA configuration profile."""

from __future__ import annotations

import argparse
from pathlib import Path

from home_location_endpoint.render import atomic_write, build_ca_profile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ca-der", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    atomic_write(args.output, build_ca_profile(args.ca_der.read_bytes()), 0o644)
    print("rendered deterministic iOS CA profile")


if __name__ == "__main__":
    main()
