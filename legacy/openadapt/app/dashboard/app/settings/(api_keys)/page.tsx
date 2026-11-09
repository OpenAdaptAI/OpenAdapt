'use client';

import React, { useEffect, useState } from 'react'
import { get } from '@/api';
import { Form } from './form';


async function getSettings(): Promise<Record<string, string>> {
    return get('/api/settings?category=api_keys', {
        cache: 'no-store',
    })
}

export default function APIKeys () {
    const [settings, setSettings] = useState({});
    useEffect(() => {
        getSettings().then(setSettings);
    }, [])

    return (
        <Form settings={settings} />
    )
}
