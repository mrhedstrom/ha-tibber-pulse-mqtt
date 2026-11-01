"""Generate companion OBIS metadata for raw MQTT fixtures."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

from tests.test_raw_fixtures import RAW_FIXTURE_DIR, generate_raw_fixture_metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay raw JSONL fixtures and write companion OBIS metadata JSON files."
    )
    parser.add_argument(
        "fixture_dir",
        nargs="?",
        type=Path,
        default=RAW_FIXTURE_DIR,
        help="Directory containing .jsonl fixtures (default: tests/fixtures/raw)",
    )
    args = parser.parse_args()

    generated_files = generate_raw_fixture_metadata(args.fixture_dir)
    for metadata_file in generated_files:
        print(metadata_file)


if __name__ == "__main__":
    main()
