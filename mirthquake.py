"""Launch the desktop workbench, or use a subcommand for headless testing."""

import sys


def main():
    if len(sys.argv) > 1:
        from quake.cli import main as cli_main
        return cli_main()
    from quake.gui import launch
    launch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
