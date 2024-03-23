# flake8: noqa # type: ignore # noqa
"""Allows for capturing the screen and audio on macOS.

This is based on: https://gist.github.com/timsutton/0c6439eb6eb1621a5964

usage: see bottom of file
"""

from ctypes import POINTER, c_char_p, c_int, c_void_p, cdll
from datetime import datetime
from sys import platform
import os

from AppKit import NSBitmapImageRep, NSImage, NSImageCompressionFactor, NSPNGFileType
from CoreMedia import CMSampleBufferGetImageBuffer
from Foundation import NSURL, NSAutoreleasePool, NSLog, NSObject
from Quartz import CIImage
from Quartz.CoreGraphics import CGMainDisplayID
import AVFoundation as AVF
import objc

from openadapt import config

NULL_PTR = POINTER(c_int)()

SAVE_IMAGES = None

_lib = cdll.LoadLibrary("/usr/lib/system/libdispatch.dylib")
_dispatch_queue_create = _lib.dispatch_queue_create
_dispatch_queue_create.argtypes = [c_char_p, c_void_p]
_dispatch_queue_create.restype = c_void_p


def dispatch_queue_create(name):
    """Creates a new dispatch queue to which you can submit blocks."""
    # https://developer.apple.com/documentation/dispatch/1453030-dispatch_queue_create
    b_name = name.encode("utf-8")
    c_name = c_char_p(b_name)
    queue = _dispatch_queue_create(c_name, NULL_PTR)

    return queue


class VideoDelegate(NSObject):
    """Delegate for the video capture."""

    def captureOutput_didOutputSampleBuffer_fromConnection_(
        self, output, sampleBuffer, connection
    ):
        """Capture the output of the video."""

        # NSLog("Delegate method called")
        imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer)
        ciImage = CIImage.imageWithCVImageBuffer_options_(imageBuffer, None)
        bitmapRep = NSBitmapImageRep.alloc().initWithCIImage_(ciImage)
        dict = {NSImageCompressionFactor: 1.0}
        pngData = bitmapRep.representationUsingType_properties_(NSPNGFileType, dict)

        if SAVE_IMAGES:
            if not os.path.exists("captures"):
                os.makedirs("captures")
            pngData.writeToFile_atomically_(
                os.path.join("captures", "frame{}.png".format(datetime.now())), False
            )


class Capture:
    """Capture the screen on macOS."""

    def __init__(self) -> None:
        """Initialize the capture object."""
        if platform != "darwin":
            raise NotImplementedError(
                "This is the macOS implementation, please use the Windows version"
            )

        objc.options.structs_indexable = True

    def start(self, save_images=False) -> None:
        """Start capture"""
        global SAVE_IMAGES
        SAVE_IMAGES = save_images
        self.display_id = CGMainDisplayID()
        self.session = AVF.AVCaptureSession.alloc().init()
        self.screen_input = AVF.AVCaptureScreenInput.alloc().initWithDisplayID_(
            self.display_id
        )

        self.delegate = VideoDelegate.alloc().init()
        self.pool = NSAutoreleasePool.alloc().init()

        self.dispatch_queue = dispatch_queue_create("oa_queue")
        self.video_data_output = AVF.AVCaptureVideoDataOutput.alloc().init()
        self.queue_ptr = objc.objc_object(c_void_p=self.dispatch_queue)

        self.video_data_output.setSampleBufferDelegate_queue_(
            self.delegate, self.queue_ptr
        )

        if self.session.canAddInput_(self.screen_input):
            self.session.addInput_(self.screen_input)

        if self.session.canAddOutput_(self.video_data_output):
            self.session.addOutput_(self.video_data_output)

        self.session.startRunning()

    def stop(self) -> None:
        """Stop capturing the screen."""
        self.session.stopRunning()
        if self.pool:
            del self.pool

class MovieCapture:
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
                        config.CAPTURE_DIR_PATH,
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
    capture.start(save_images=True)
    input("Press enter to stop")
    capture.stop()
