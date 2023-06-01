"""
Implements a ReplayStrategy mixin using Pypotrace to convert an image to SVG text.

NOTE: additional libraries on the system are needed,
        see https://github.com/flupke/pypotrace#installation

Usage:

    class MyReplayStrategy(SVGReplayStrategyMixin):
        ...
"""
from openadapt.pypotrace import potrace
import numpy as np
import drawsvg as draw

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy


class SVGReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin that uses Pypotrace to convert an image to SVG text.

    Attributes:
        recording: the recording to be played back

    """
    def __init__(self, recording: Recording):
        super().__init__(recording)

    def get_svg_text(self, screenshot: Screenshot) -> str:
        png_img = screenshot.image

        # convert png to pbm
        pbm_img = png_img.convert("1")

        # trace pbm
        image_array = np.array(pbm_img)
        bitmap = potrace.Bitmap(image_array)
        path = bitmap.trace()

        # convert trace into svg text
        img_width, img_height = png_img.size
        svg = draw.Drawing(img_width, img_height, origin=(0, 0))
        svg.set_pixel_scale()
        for curve in path.curves:
            curr_x, curr_y = curve.start_point
            for segment in curve.segments:
                end_x, end_y = segment.end_point
                if segment.is_corner:
                    middle_x, middle_y = segment.c
                    lines_in_the_segment = draw.Lines(curr_x, curr_y, middle_x, middle_y,
                                                      end_x, end_y, fill="none", stroke="black")
                    svg.append(lines_in_the_segment)
                else:
                    start_control_x, start_control_y = segment.c1
                    end_control_x, end_control_y = segment.c2
                    segment_path = draw.Path(stroke="black", fill="none")
                    segment_path.C(start_control_x, start_control_y, end_control_x, end_control_y,
                                   end_x, end_y)
                    svg.append(segment_path)
                curr_x, curr_y = end_x, end_y

        return svg.as_svg()
