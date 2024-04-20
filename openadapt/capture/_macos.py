"""Allows for capturing the screen and audio on macOS.

This is based on: https://gist.github.com/timsutton/0c6439eb6eb1621a5964

usage: see bottom of file
"""
from datetime import datetime
from sys import platform
import os

from Foundation import NSURL, NSObject  # type: ignore # noqa
from Quartz import CGMainDisplayID  # type: ignore # noqa
import AVFoundation as AVF  # type: ignore # noqa
import objc  # type: ignore # noqa

from openadapt.config import CAPTURE_DIR_PATH


class Capture:
    """Capture the screen, audio, and camera on macOS."""

    def __init__(self) -> None:
        """Initialize the capture object."""
        if platform != "darwin":
            raise NotImplementedError(
                "This is the macOS implementation, please use the Windows version"
            )

        objc.options.structs_indexable = True

    def start(self, audio: bool = False, camera: bool = False) -> None:
        """Start capturing the screen, audio, and camera.

        Args:
            audio (bool, optional): Whether to capture audio (default: False).
            camera (bool, optional): Whether to capture the camera (default: False).
        """
        self.display_id = CGMainDisplayID()
        self.session = AVF.AVCaptureSession.alloc().init()
        self.screen_input = AVF.AVCaptureScreenInput.alloc().initWithDisplayID_(
            self.display_id
        )
        self.file_output = AVF.AVCaptureMovieFileOutput.alloc().init()
        self.camera_session = None  # not used if camera=False

        # Create an audio device input with the default audio device
        self.audio_input = AVF.AVCaptureDeviceInput.alloc().initWithDevice_error_(
            AVF.AVCaptureDevice.defaultDeviceWithMediaType_(AVF.AVMediaTypeAudio), None
        )

        if not os.path.exists(CAPTURE_DIR_PATH):
            os.mkdir(CAPTURE_DIR_PATH)
        self.file_url = NSURL.fileURLWithPath_(
            os.path.join(
                CAPTURE_DIR_PATH,
                datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".mov",
            )
        )
        if audio and self.session.canAddInput_(self.audio_input[0]):
            self.session.addInput_(self.audio_input[0])

        if self.session.canAddInput_(self.screen_input):
            self.session.addInput_(self.screen_input)

        self.session.addOutput_(self.file_output)

        self.session.startRunning()

        # Cheat and pass a dummy delegate object where
        # normally we'd have a AVCaptureFileOutputRecordingDelegate
        self.file_url = (
            self.file_output.startRecordingToOutputFileURL_recordingDelegate_(
                self.file_url, NSObject.alloc().init()
            )
        )

        if camera:
            self._use_camera()

    def _use_camera(self) -> None:
        """Start capturing the camera."""
        self.camera_session = AVF.AVCaptureSession.alloc().init()
        self.camera_file_output = AVF.AVCaptureMovieFileOutput.alloc().init()
        self.camera_input = AVF.AVCaptureDeviceInput.alloc().initWithDevice_error_(
            AVF.AVCaptureDevice.defaultDeviceWithMediaType_(AVF.AVMediaTypeVideo), None
        )

        if self.camera_session.canAddInput_(self.camera_input[0]):
            self.camera_session.addInput_(self.camera_input[0])
        self.camera_session.startRunning()

        self.camera_session.addOutput_(self.camera_file_output)

        self.camera_url = (
            self.camera_file_output.startRecordingToOutputFileURL_recordingDelegate_(
                NSURL.fileURLWithPath_(
                    os.path.join(
                        CAPTURE_DIR_PATH,
                        datetime.now().strftime("camera.%Y-%m-%d-%H-%M-%S") + ".mov",
                    )
                ),
                NSObject.alloc().init(),
            )
        )

    def stop(self) -> None:
        """Stop capturing the screen, audio, and camera."""
        self.session.stopRunning()
        if self.camera_session:
            self.camera_session.stopRunning()


if __name__ == "__main__":
    capture = Capture()
    capture.start(audio=True, camera=False)
    input("Press enter to stop")
    capture.stop()
