"""Allow ``python -m latex_word_review`` to invoke the CLI."""

from latex_word_review.cli import main

if __name__ == "__main__":  # pragma: no cover - exercised through a subprocess test
    raise SystemExit(main())
