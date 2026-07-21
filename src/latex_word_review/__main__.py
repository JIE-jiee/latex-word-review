"""Allow ``python -m latex_word_review`` to invoke the CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

from latex_word_review.errors import ContractError, ErrorCode, ExitCode
from latex_word_review.worker_dispatch import dispatch_internal_worker

_GUI_EXECUTABLE_STEM: Final = "latexwordreview"


def main() -> int:
    """Dispatch an internal worker before the GUI or CLI argument parser."""

    worker_code = dispatch_internal_worker(sys.argv[1:])
    if worker_code is not None:
        return worker_code
    is_gui_executable = (
        bool(getattr(sys, "frozen", False))
        and Path(sys.executable).stem.casefold() == _GUI_EXECUTABLE_STEM
    )
    if is_gui_executable and len(sys.argv) == 1:
        from latex_word_review.app_server import serve_app
        from latex_word_review.windows_dialogs import show_startup_failure

        try:
            serve_app()
        except Exception as exc:
            if isinstance(exc, ContractError):
                error_code = exc.code.value
                exit_code = int(exc.exit_code)
            else:
                error_code = ErrorCode.INTERNAL_INVARIANT.value
                exit_code = int(ExitCode.INTERNAL)
            show_startup_failure(error_code)
            return exit_code
        return int(ExitCode.SUCCESS)

    from latex_word_review.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":  # pragma: no cover - exercised through a subprocess test
    raise SystemExit(main())
