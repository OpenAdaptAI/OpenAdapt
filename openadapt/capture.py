"""
Allows for capturing the screen and audio on macOS.

This is based on: https://gist.github.com/timsutton/0c6439eb6eb1621a5964

usage: see bottom of file
"""
import os
from datetime import datetime
from sys import platform

import AVFoundation as AVF
import objc
from Foundation import NSURL, NSObject
from Quartz import CGMainDisplayID

from openadapt import config


class Capture:
    def __init__(self):
        # only on macos
        if platform != "darwin":
            raise NotImplementedError("Only implemented on macOS")

        objc.options.structs_indexable = True

    def start(self, audio=False, camera=False):
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

        if not os.path.exists(config.CAPTURE_DIR_PATH):
            os.mkdir(config.CAPTURE_DIR_PATH)
        self.file_url = NSURL.fileURLWithPath_(
            os.path.join(
                config.CAPTURE_DIR_PATH,
                datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".mov",
            )
        )
        if audio and self.session.canAddInput_(self.audio_input[0]):
            self.session.addInput_(self.audio_input[0])

        if self.session.canAddInput_(self.screen_input):
            self.session.addInput_(self.screen_input)

        self.session.addOutput_(self.file_output)

        self.session.startRunning()

        # Cheat and pass a dummy delegate object where normally we'd have a AVCaptureFileOutputRecordingDelegate
        self.file_url = (
            self.file_output.startRecordingToOutputFileURL_recordingDelegate_(
                self.file_url, NSObject.alloc().init()
            )
        )

        if camera:
            self._use_camera()

    def _use_camera(self):
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
                        config.CAPTURE_DIR_PATH,
                        datetime.now().strftime("camera.%Y-%m-%d-%H-%M-%S") + ".mov",
                    )
                ),
                NSObject.alloc().init(),
            )
        )

    def stop(self):
        self.session.stopRunning()
        if self.camera_session:
            self.camera_session.stopRunning()


if __name__ == "__main__":
    capture = Capture()
    capture.start(audio=True, camera=True)
    input("Press enter to stop")
    capture.stop()
