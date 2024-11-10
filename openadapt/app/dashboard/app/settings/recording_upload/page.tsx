'use client'

import React, { useEffect, useState } from 'react'
import { getSettings } from '@/app/utils'
import { Form } from './form'
import { RecordingUploadSettings } from '../utils'

export default function RecordingUpload() {
    const [settings, setSettings] = useState<RecordingUploadSettings>({
        OVERWRITE_RECORDING_DESTINATION: false,
        RECORDING_PUBLIC_KEY: '',
        RECORDING_PRIVATE_KEY: '',
        RECORDING_BUCKET_NAME: '',
        RECORDING_BUCKET_REGION: '',
    })
    useEffect(() => {
        getSettings<RecordingUploadSettings>('recording_upload').then(
            setSettings
        )
    }, [])
    return <Form settings={settings} />
}
