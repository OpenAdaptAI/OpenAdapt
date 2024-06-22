'use client';


import { Box, Button, Stack, Text, TextInput } from '@mantine/core'
import { isNotEmpty, useForm } from '@mantine/form'
import { notifications } from '@mantine/notifications'
import React, { useEffect } from 'react'

export const RegisterForUpdates = () => {
    const onboardingForm = useForm({
        initialValues: {
            email: '',
        },
        validate: {
          email: isNotEmpty('Email is required'),
        }
    })
    useEffect(() => {
        fetch('/api/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                REDIRECT_TO_ONBOARDING: false
            }),
        })
    }, [])
    function onSubmit({ email }: { email: string }) {
        fetch('https://openadapt.ai/form.html', {
            method: 'POST',
            mode: 'no-cors',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: new URLSearchParams({
                email,
                'form-name': 'email',
                'bot-field': '',
            }).toString(),
        }).then(() => {
            notifications.show({
                title: 'Thank you!',
                message: 'You have been registered for updates',
                color: 'green',
            })
        })
    }
    return (
        <Box maw={600}>
            <Stack gap="xs">
                <form onSubmit={onboardingForm.onSubmit(onSubmit)}>
                    <TextInput
                        placeholder="Email (optional)"
                        label="Register for updates"
                        description="We promise not to spam!"
                        type="email"
                        {...onboardingForm.getInputProps('email')}
                    />
                    <Button type="submit" mt={10}>
                        Register
                    </Button>
                </form>
            </Stack>
        </Box>
    )
}
