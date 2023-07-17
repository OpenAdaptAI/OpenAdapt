"""
Implements a ReplayStrategy mixin using VTracer to convert an image to SVG text.

Usage:

    class MyReplayStrategy(SVGReplayStrategyMixin):
        ...
"""
import os
import platform
import subprocess
import tempfile

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy


class SVGReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin that uses VTracer to convert an image to SVG text.

    Attributes:
        recording: the recording to be played back
        path_to_vtracer: the absolute path to the cargo command
    """
    def __init__(self, recording: Recording, vtracer_executable: str = "vtracer"):
        if not is_dependency_installed(vtracer_executable):
            if not is_dependency_installed("cargo"):
                root_directory = os.path.expanduser('~')
                
                # install rust
                if platform.system() == "Darwin":
                    subprocess.run(["brew", "install", "rust"])
                elif platform.system() == "Windows":
                    # windows
                    cwd = os.getcwd()
                    os.chdir(root_directory)
                    subprocess.run(["curl", "--proto", "'=https'", "--tlsv1.2", "-sSf", "https://sh.rustup.rs", "|", "sh"])
                    os.chdir(cwd)
                else:
                    raise Exception(f"Unsupported {platform.system()=}")
            path_to_cargo = os.path.join(root_directory, ".cargo/bin")
            # install vtracer
            subprocess.run([path_to_cargo, "install", "vtracer"])
            self.path_to_vtracer = os.path.join(path_to_cargo, "vtracer")
        else:
            self.path_to_vtracer = vtracer_executable

        super().__init__(recording)

    def get_svg_text(self, screenshot: Screenshot) -> str:
        input_img = screenshot.image

        temp_output = tempfile.NamedTemporaryFile(delete=False, suffix='.svg')

        # Create a temporary png file with the image
        #   delete=False so the file is deleted after the function returns
        with tempfile.NamedTemporaryFile(suffix='.png') as temp_input:
            # Save the image to the temporary file
            input_img.save(temp_input, 'PNG')
            subprocess.run([self.path_to_vtracer, "--input", temp_input.name, "--output", temp_output.name])

        with open(temp_output.name, "r") as file:
            svg_text = file.read()

        return svg_text

def is_dependency_installed(dependency_name: str):
    try:
        # Run the command to check if the dependency is installed
        subprocess.check_output([dependency_name, "--version"], check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False