'use client';

import { get } from '@/api';
import { Button, Checkbox, Divider, Grid, Modal, Stack, Text, TextInput } from '@mantine/core';
import { useForm } from '@mantine/form';
import Link from 'next/link';
import React, { useEffect, useState } from 'react'

export const Onboarding = () => {
    const [showOnboardingModal, setShowOnboardingModal] = useState(false);
    const onboardingForm = useForm({
        initialValues: {
            email: '',
        },
    });

    async function fetchOnboardingStatus() {
        const resp = await get<{ SHOW_ONBOARDING_MODAL: boolean }>('/api/settings?category=general', {
            cache: 'no-store',
        })
        return resp.SHOW_ONBOARDING_MODAL;
    }

    function onClose() {
        fetch('/api/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ SHOW_ONBOARDING_MODAL: false }),
        }).then(() => setShowOnboardingModal(false));
    }

    function onSubmit({ email }: { email: string }) {
        fetch('https://openadapt.ai/form.html', {
            method: 'POST',
            mode: 'no-cors',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: new URLSearchParams({ email, 'form-name': 'email', 'bot-field': '' }).toString(),
        }).then(() => {
            onboardingForm.reset();
            onClose();
        });
    }


    useEffect(() => {
        fetchOnboardingStatus().then(setShowOnboardingModal);
    }, []);

    return (
        <Modal opened={showOnboardingModal} onClose={onClose} size="md" centered title="Welcome to OpenAdapt">
            <p>Let's get started with a quick onboarding process.</p>
            <Stack gap="xs">
                <form onSubmit={onboardingForm.onSubmit(onSubmit)}>
                    <TextInput placeholder="Email (optional)" description="Register for updates (we promise not to spam)" type='email' {...onboardingForm.getInputProps('email')} />
                    <Button type='submit' mt={10}>Register</Button>
                </form>
            </Stack>
            <Text mt={10} size='xs'>Register for updates (we promise not to spam)</Text>
            <Divider my={10} />
            <Text size='sm'>
                <Link href="https://www.getclockwise.com/c/richard-abrich/quick-meeting" className='no-underline'>
                    Book a call with us
                </Link>
                <Text component='span'> to discuss how OpenAdapt can help your team</Text>
            </Text>
        </Modal>
    )
}
