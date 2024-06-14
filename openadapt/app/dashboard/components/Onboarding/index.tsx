'use client';

import { get } from '@/api';
import { Button, Checkbox, Divider, Grid, Group, Modal, Stack, Stepper, Text, TextInput } from '@mantine/core';
import { useForm } from '@mantine/form';
import Link from 'next/link';
import React, { useEffect, useState } from 'react'
import { Tutorial } from './steps/Tutorial';
import { RegisterForUpdates } from './steps/RegisterForUpdates';
import { BookACall } from './steps/BookACall';
import { CompleteOnboarding } from './steps/CompleteOnboarding';

export const Onboarding = () => {
    const [showOnboardingModal, setShowOnboardingModal] = useState(false);
    const [active, setActive] = useState(1);
    const nextStep = () => setActive((current) => (current < 3 ? current + 1 : current));
    const prevStep = () => setActive((current) => (current > 0 ? current - 1 : current));

    async function fetchOnboardingStatus() {
        const resp = await get<{ SHOW_ONBOARDING_MODAL: boolean }>('/api/settings?category=general', {
            cache: 'no-store',
        })
        return resp.SHOW_ONBOARDING_MODAL;
    }

    function completeOnboarding() {
        fetch('/api/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ SHOW_ONBOARDING_MODAL: false }),
        }).then(() => setShowOnboardingModal(false));
    }


    useEffect(() => {
        fetchOnboardingStatus().then(setShowOnboardingModal);
    }, []);

    return (
        <Modal opened={showOnboardingModal} onClose={completeOnboarding} size="90vw" centered title="Welcome to OpenAdapt" classNames={{
            content: 'h-[90vh]'
        }}>
            <Stepper active={active} onStepClick={setActive} mt={20}>
                <Stepper.Step label="How to use OpenAdapt" description="Watch a short tutorial" allowStepClick={false} allowStepSelect={false}>
                    <Tutorial />
                </Stepper.Step>
                <Stepper.Step label="Register for updates" description="Stay up to date" allowStepClick={false} allowStepSelect={false}>
                    <RegisterForUpdates />
                </Stepper.Step>
                <Stepper.Step label="Book a call" description="Discuss your needs" allowStepClick={false} allowStepSelect={false}>
                    <BookACall />
                </Stepper.Step>
                <Stepper.Completed>
                    <CompleteOnboarding completeOnboarding={completeOnboarding} />
                </Stepper.Completed>
            </Stepper>
            {active < 3 && (
                <Group justify="center" mt="xl" pos="absolute" bottom={50} left="calc(50% - 78px)">
                    <Button variant="default" onClick={prevStep}>Back</Button>
                    <Button onClick={nextStep}>Next</Button>
                </Group>
            )}
        </Modal>
    )
}
