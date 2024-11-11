'use client'

import React, { useEffect, useState } from 'react'
import { Form } from './form'
import { getRecordingUploadSettings } from '../utils'

export default function RecordingUpload() {
    const [settings, setSettings] = useState<
        Awaited<ReturnType<typeof getRecordingUploadSettings>>
    >({
        OVERWRITE_RECORDING_DESTINATION: false,
        RECORDING_PUBLIC_KEY: '',
        RECORDING_PRIVATE_KEY: '',
        RECORDING_BUCKET_NAME: '',
        RECORDING_BUCKET_REGION: '',
        RECORDING_DELETION_ENABLED: false,
    })
    useEffect(() => {
        getRecordingUploadSettings().then(setSettings)
    }, [])
    return <Form settings={settings} />
}
