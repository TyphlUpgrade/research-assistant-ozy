"""Module entrypoint — `python -m research_assistant <subcommand>` delegates here."""
import sys

from research_assistant.cli import main

if __name__ == "__main__":
    sys.exit(main())
