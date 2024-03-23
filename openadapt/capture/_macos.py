# flake8: noqa # type: ignore # noqa
"""Allows for capturing the screen and audio on macOS.

This is based on: https://gist.github.com/timsutton/0c6439eb6eb1621a5964

usage: see bottom of file
"""

from ctypes import POINTER, c_char_p, c_int, c_void_p, cdll
from datetime import datetime
from sys import platform
import os

from Foundation import NSObject, NSLog, NSAutoreleasePool
from Quartz import CIImage
from CoreMedia import CMSampleBufferGetImageBuffer
from Quartz.CoreGraphics import CGMainDisplayID
from AppKit import NSBitmapImageRep, NSImage, NSPNGFileType, NSImageCompressionFactor
import AVFoundation as AVF
import objc


NULL_PTR = POINTER(c_int)()

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

    def start(self) -> None:
        """Start capture"""

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
        """Stop capturing the screen, audio, and camera."""
        self.session.stopRunning()
        if self.pool:
            del self.pool


if __name__ == "__main__":
    capture = Capture()
    capture.start()
    input("Press enter to stop")
    capture.stop()
