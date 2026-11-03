from __future__ import annotations

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from prediction_market_extensions import install_commission_patch
from prediction_market_extensions.backtesting._timing_harness import timing_harness
from prediction_market_extensions.backtesting.longshot.cli import main

install_commission_patch()


@timing_harness
def run() -> None:
    main()


if __name__ == "__main__":
    run()
