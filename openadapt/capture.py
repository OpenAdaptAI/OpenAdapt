import time

import AVFoundation as AVF
import Quartz
from Foundation import NSURL, NSObject


def main():
    display_id = Quartz.CGMainDisplayID()

    session = AVF.AVCaptureSession.alloc().init()
    screen_input = AVF.AVCaptureScreenInput.alloc().initWithDisplayID_(display_id)
    file_output = AVF.AVCaptureMovieFileOutput.alloc().init()

    # Create an audio device input with the default audio device
    # create AVCaptureDeviceDiscoverySession
    
    audio_input = AVF.AVCaptureDeviceInput.alloc().initWithDevice_error_(
        audio_device, None
    )

    # Add the audio input to the session
    if session.canAddInput_(audio_input):
        session.addInput_(audio_input)

    session.addInput_(screen_input)
    session.addOutput_(file_output)
    session.startRunning()

    file_url = NSURL.fileURLWithPath_("foo.mov")
    # Cheat and pass a dummy delegate object where normally we'd have a
    # AVCaptureFileOutputRecordingDelegate
    file_url = file_output.startRecordingToOutputFileURL_recordingDelegate_(
        file_url, NSObject.alloc().init()
    )
    time.sleep(10)
    session.stopRunning()


if __name__ == "__main__":
    main()
