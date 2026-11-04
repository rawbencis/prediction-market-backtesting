from __future__ import annotations

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from prediction_market_extensions.backtesting.longshot.post_entry import main

if __name__ == "__main__":
    main()
