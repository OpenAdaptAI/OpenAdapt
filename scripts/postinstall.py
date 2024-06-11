import subprocess
import sys


def install_detectron2():
    subprocess.check_call([
        sys.executable, '-m', 'pip', 'install',
        'git+https://github.com/facebookresearch/detectron2.git',
        '--no-build-isolation'
    ])


def main():
    install_detectron2()


if __name__ == '__main__':
    main()
