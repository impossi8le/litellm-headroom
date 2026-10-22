"""Write litellm-config.yaml from models.yaml."""
import argparse
import sys
from pathlib import Path

import routing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent),
                        help="directory holding models.yaml (default: script directory)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    models_path = root / "models.yaml"
    if not models_path.exists():
        return 0

    try:
        models = routing.load_and_validate(models_path)
    except routing.ConfigError as exc:
        for error in exc.errors:
            print("error: %s" % error, file=sys.stderr)
        return 1

    routing.write_config(models, root / "litellm-config.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
