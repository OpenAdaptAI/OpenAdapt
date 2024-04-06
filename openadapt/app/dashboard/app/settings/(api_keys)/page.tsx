import React from 'react'
import { get } from '@/api';
import { Form } from './form';


async function getSettings(): Promise<Record<string, string>> {
    return get('/api/settings?category=api_keys', {
        cache: 'no-store',
    })
}

export default async function APIKeys () {
    const settings = await getSettings();
    return (
        <Form settings={settings} />
    )
}
