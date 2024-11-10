import { UseFormReturnType } from '@mantine/form'
import { notifications } from '@mantine/notifications'

export function validateScrubbingSettings(settings: Record<string, string>) {
    const errors: Record<string, string> = {}
    if (settings.SCRUB_ENABLED) {
        return errors
    }
    if (settings.SCRUB_CHAR.length === 0) {
        errors.SCRUB_CHAR = 'Scrubbing character is required'
    }
    if (settings.SCRUB_CHAR.length > 1) {
        errors.SCRUB_CHAR = 'Scrubbing character must be a single character'
    }
    if (settings.SCRUB_LANGUAGE.length === 0) {
        errors.SCRUB_LANGUAGE = 'Scrubbing language is required'
    }
    if (settings.SCRUB_LANGUAGE.length > 2) {
        errors.SCRUB_LANGUAGE =
            'Scrubbing language must be a two character language code'
    }

    return errors
}

export function validateRecordAndReplaySettings(
    settings: Record<string, string>
) {
    const errors: Record<string, string> = {}
    if (settings.VIDEO_PIXEL_FORMAT.length === 0) {
        errors.VIDEO_PIXEL_FORMAT = 'Video pixel format is required'
    }
    return errors
}

export function validateRecordingUploadSettings(
    settings: RecordingUploadSettings
) {
    const errors: Record<string, string> = {}
    if (settings.OVERWRITE_RECORDING_DESTINATION) {
        if (settings.RECORDING_PUBLIC_KEY.length === 0) {
            errors.RECORDING_PUBLIC_KEY =
                'Recording destination public key is required'
        }
        if (settings.RECORDING_PRIVATE_KEY.length === 0) {
            errors.RECORDING_PRIVATE_KEY =
                'Recording destination private key is required'
        }
        if (settings.RECORDING_BUCKET_NAME.length === 0) {
            errors.RECORDING_BUCKET_NAME =
                'Recording destination bucket name is required'
        }
        if (settings.RECORDING_BUCKET_REGION.length === 0) {
            errors.RECORDING_BUCKET_REGION =
                'Recording destination bucket region is required'
        }
    }
    console.log(settings)
    return errors
}

export function saveSettings(form: UseFormReturnType<any>) {
    return function (values: Record<string, any>) {
        fetch('/api/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(values),
        })
            .then((resp) => {
                if (resp.ok) {
                    notifications.show({
                        title: 'Settings saved',
                        message: 'Your settings have been saved',
                        color: 'green',
                    })
                    return resp.json()
                } else {
                    notifications.show({
                        title: 'Failed to save settings',
                        message: 'Please try again',
                        color: 'red',
                    })
                    return null
                }
            })
            .then((resp) => {
                if (!resp) {
                    return
                }
                form.setInitialValues(values)
                form.setDirty({})
            })
    }
}

export type RecordingUploadSettings = {
    OVERWRITE_RECORDING_DESTINATION: boolean
    RECORDING_PUBLIC_KEY: string
    RECORDING_PRIVATE_KEY: string
    RECORDING_BUCKET_NAME: string
    RECORDING_BUCKET_REGION: string
}
