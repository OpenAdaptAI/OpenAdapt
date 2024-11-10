'use client'

import React, { useEffect, useState } from 'react'
import { get } from '@/api'
import { Form } from './form'
import { getSettings } from '@/app/utils'

export default function APIKeys() {
    const [settings, setSettings] = useState({})
    useEffect(() => {
        getSettings('scrubbing').then(setSettings)
    }, [])
    return <Form settings={settings} />
}
