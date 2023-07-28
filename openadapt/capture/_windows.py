"""Allows for capturing the screen and audio on Windows."""
from sys import platform
import datetime
import os
import threading
import time

import cv2
import numpy as np
import win32api  # type: ignore # noqa
import win32con  # type: ignore # noqa
import win32gui  # type: ignore # noqa
import win32ui  # type: ignore # noqa

from openadapt import config


class Capture:
    """Capture the screen video on Windows."""

    def __init__(self) -> None:
        """Initialize the capture object."""
        if platform != "win32":
            raise NotImplementedError(
                "This is the Windows implementation, please use the macOS version"
            )
        self.hwin = win32gui.GetDesktopWindow()
        self.desktop_handle = win32gui.GetDesktopWindow()
        self.window = {
            "left": 0,
            "top": 0,
            "width": win32api.GetSystemMetrics(win32con.SM_CXSCREEN),
            "height": win32api.GetSystemMetrics(win32con.SM_CYSCREEN),
        }
        self.is_recording = False
        self.video_out = None
        self.frame_interval = 1 / 20  # 20 FPS

    def start(self, audio: bool = True) -> None:
        """Start capturing the screen video.

        TODO: add audio support
        Args:
            audio (bool): Whether to capture audio. )
        """
        self.is_recording = True
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        video_outpath = os.path.join(
            config.CAPTURE_DIR_PATH,
            datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".mov",
        )
        self.video_out = cv2.VideoWriter(
            video_outpath,
            fourcc,
            20.0,
            (self.window["width"], self.window["height"]),
        )

        def record_screen() -> None:
            """Record the screen and save it to a file."""
            hwindc = win32gui.GetWindowDC(self.hwin)
            srcdc = win32ui.CreateDCFromHandle(hwindc)
            memdc = srcdc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(
                srcdc, self.window["width"], self.window["height"]
            )
            memdc.SelectObject(bmp)
            prev_time = time.time()
            while self.is_recording:
                memdc.BitBlt(
                    (0, 0),
                    (self.window["width"], self.window["height"]),
                    srcdc,
                    (self.window["left"], self.window["top"]),
                    win32con.SRCCOPY,
                )
                signedIntsArray = bmp.GetBitmapBits(True)
                img = np.frombuffer(signedIntsArray, dtype="uint8")
                img.shape = (self.window["height"], self.window["width"], 4)
                # Drop the alpha channel to get RGB format
                img = img[..., :3]
                self.video_out.write(img)
                curr_time = time.time()
                elapsed_time = curr_time - prev_time
                prev_time = curr_time
                time.sleep(max(0, self.frame_interval - elapsed_time))
            print("Screen recording finished.")
            srcdc.DeleteDC()
            memdc.DeleteDC()
            win32gui.ReleaseDC(self.hwin, hwindc)
            win32gui.DeleteObject(bmp.GetHandle())

        screen_thread = threading.Thread(target=record_screen)
        screen_thread.start()

    def stop(self) -> None:
        """Stop capturing the screen video."""
        self.is_recording = False
        if self.video_out:
            self.video_out.release()


if __name__ == "__main__":
    capture = Capture()
    capture.start()
    input("Press enter to stop")
    capture.stop()
