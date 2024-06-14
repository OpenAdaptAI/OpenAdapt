'use client';

import { get } from '@/api';
import { Box, Divider, Modal } from '@mantine/core';
import React, { useEffect, useState } from 'react'
import { Tutorial } from './steps/Tutorial';
import { RegisterForUpdates } from './steps/RegisterForUpdates';
import { BookACall } from './steps/BookACall';

export const Onboarding = () => {
    const [showOnboardingModal, setShowOnboardingModal] = useState(false);

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
            <Box w={"70vw"} mx="auto">
                <Tutorial />
                <Divider my={20} w="40vw" mx="auto" />
                <RegisterForUpdates />
                <Divider my={20} w="40vw" mx="auto" />
                <BookACall />
            </Box>
        </Modal>
    )
}
