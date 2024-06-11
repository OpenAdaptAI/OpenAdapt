"""Post-install script.

TODO: consolidate with install-dashbaord.
"""


import subprocess
import sys


def install_detectron2() -> None:
    """Install detectron2."""
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "git+https://github.com/facebookresearch/detectron2.git",
            "--no-build-isolation",
        ]
    )


def main() -> None:
    """Main."""
    install_detectron2()


if __name__ == "__main__":
    main()
