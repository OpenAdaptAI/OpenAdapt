"""Allows for capturing the screen and audio on macOS.

This is based on: https://gist.github.com/timsutton/0c6439eb6eb1621a5964

usage: see bottom of file
"""

from datetime import datetime
from sys import platform
import os

from Foundation import NSURL, NSObject  # type: ignore # noqa
from Quartz import CGMainDisplayID  # type: ignore # noqa
import Quartz.CoreVideo  # type: ignore # noqa
import Quartz.CoreGraphics  # type: ignore # noqa
from AppKit import NSImage, NSBitmapImageRep  # type: ignore # noqa
import AVFoundation as AVF  # type: ignore # noqa
import objc  # type: ignore # noqa
import concurrent.futures

from openadapt import config


class VideoDelegate(NSObject):
    """Delegate for the video capture."""

    def captureOutput_didOutputSampleBuffer_fromConnection_(
        self, output, sampleBuffer, connection
    ):
        image_buffer = AVF.CMSampleBufferGetImageBuffer(sampleBuffer)
        Quartz.CoreVideo.CVPixelBufferLockBaseAddress(
            image_buffer, Quartz.CoreVideo.kCVPixelBufferLock_ReadOnly
        )
        width = Quartz.CoreVideo.CVPixelBufferGetWidth(image_buffer)
        height = Quartz.CoreVideo.CVPixelBufferGetHeight(image_buffer)
        color_space = Quartz.CoreGraphics.CGColorSpaceCreateDeviceRGB()
        context = Quartz.CoreGraphics.CGBitmapContextCreate(
            Quartz.CoreVideo.CVPixelBufferGetBaseAddress(image_buffer),
            width,
            height,
            8,
            Quartz.CoreVideo.CVPixelBufferGetBytesPerRow(image_buffer),
            color_space,
            Quartz.CoreGraphics.kCGImageAlphaNoneSkipFirst,
        )
        quartz_image = Quartz.CoreGraphics.CGBitmapContextCreateImage(context)
        Quartz.CoreVideo.CVPixelBufferUnlockBaseAddress(
            image_buffer, Quartz.CoreVideo.kCVPixelBufferLock_ReadOnly
        )
        image = NSImage.alloc().initWithCGImage_size_(quartz_image, (width, height))
        bitmap_rep = NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation())
        if not os.path.exists(config.CAPTURE_DIR_PATH):
            os.mkdir(config.CAPTURE_DIR_PATH)
        self.file_url = NSURL.fileURLWithPath_(
            os.path.join(
                config.CAPTURE_DIR_PATH,
                datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".tiff",
            )
        )
        file_path = self.file_url.path()
        bitmap_rep.TIFFRepresentation().writeToFile_atomically_(file_path, True)


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
        self.video_data_output = AVF.AVCaptureVideoDataOutput.alloc().init()
        self.camera_session = None  # not used if camera=False

        queue = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        delegate = VideoDelegate.alloc().init()

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

        self.video_data_output.setSampleBufferDelegate_queue_(delegate, queue)
        if self.session.canAddOutput_(self.video_data_output):
            self.session.addOutput_(self.video_data_output)

        if audio and self.session.canAddInput_(self.audio_input[0]):
            self.session.addInput_(self.audio_input[0])

        if self.session.canAddInput_(self.screen_input):
            self.session.addInput_(self.screen_input)

        self.session.startRunning()

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
    capture.start(audio=True, camera=False)
    input("Press enter to stop")
    capture.stop()
