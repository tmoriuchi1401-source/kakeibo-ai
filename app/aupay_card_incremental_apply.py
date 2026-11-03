"""Checkpoint-bound entry; shares the first-window safety path and writer."""
from .aupay_card_first_window_apply import main


if __name__ == "__main__":
    raise SystemExit(main(incremental=True))
