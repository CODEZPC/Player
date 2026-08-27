"""音乐播放器入口。"""

import sys
from cmd_analyze import run_app


def main() -> None:
    initial_file = sys.argv[1] if len(sys.argv) > 1 else None
    run_app(initial_file)


if __name__ == "__main__":
    main()