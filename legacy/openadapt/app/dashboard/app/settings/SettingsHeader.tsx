'use client';

import { Text } from '@mantine/core';
import { usePathname } from 'next/navigation';
import React from 'react'


type Props = {
    routes: { name: string; path: string }[];
}

export const SettingsHeader = ({ routes }: Props) => {
    const currentRoute = usePathname()
    return (
        <Text fz={24}>Settings | {routes.find(route => route.path === currentRoute)?.name}</Text>
    )
}
