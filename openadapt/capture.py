import os
from datetime import datetime
from sys import platform

import AVFoundation as AVF
import objc
from Foundation import NSURL, NSObject
from Quartz import CGMainDisplayID


class OpenAdaptCapture:
    def __init__(self):
        # only on macos
        if platform != "darwin":
            raise NotImplementedError("Only implemented on macOS")

        objc.options.structs_indexable = True

        self.display_id = CGMainDisplayID()
        self.session = AVF.AVCaptureSession.alloc().init()
        self.screen_input = AVF.AVCaptureScreenInput.alloc().initWithDisplayID_(
            self.display_id
        )
        self.file_output = AVF.AVCaptureMovieFileOutput.alloc().init()

        # Create an audio device input with the default audio device
        self.audio_input = AVF.AVCaptureDeviceInput.alloc().initWithDevice_error_(
            AVF.AVCaptureDevice.defaultDeviceWithMediaType_(AVF.AVMediaTypeAudio), None
        )
        if not os.path.exists("captures"):
            os.mkdir("captures")
        self.file_url = NSURL.fileURLWithPath_(
            os.path.join("captures", datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".mov")
        )

    def start(self, audio=False, fpath=None):
        if audio and self.session.canAddInput_(self.audio_input[0]):
            self.session.addInput_(self.audio_input[0])
        if self.session.canAddInput_(self.screen_input):
            self.session.addInput_(self.screen_input)

        self.session.addOutput_(self.file_output)

        self.session.startRunning()

        if fpath is not None:
            self.file_url = NSURL.fileURLWithPath_(fpath)

        # Cheat and pass a dummy delegate object where normally we'd have a AVCaptureFileOutputRecordingDelegate
        self.file_url = (
            self.file_output.startRecordingToOutputFileURL_recordingDelegate_(
                self.file_url, NSObject.alloc().init()
            )
        )

    def stop(self):
        self.session.stopRunning()


if __name__ == "__main__":
    capture = OpenAdaptCapture()
    capture.start(audio=True)
    input("Press enter to stop")
    capture.stop()
