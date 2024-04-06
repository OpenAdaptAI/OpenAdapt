import React from 'react'
import { get } from '@/api';
import { Form } from './form';


async function getSettings(): Promise<Record<string, string>> {
    return get('/api/settings?category=record_and_replay', {
        cache: 'no-store',
    })
}

export default async function APIKeys () {
    const settings = await getSettings();
    return (
        <Form settings={settings} />
    )
}
