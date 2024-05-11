'use client'

import { AppShell, Burger, Image, Text } from '@mantine/core'
import React from 'react'
import { Navbar } from '../Navbar'
import { useDisclosure } from '@mantine/hooks'
import logo from '../../assets/logo.png'

type Props = {
    children: React.ReactNode
}

export const Shell = ({ children }: Props) => {
    const [opened, { toggle }] = useDisclosure()
    return (
        <AppShell
            header={{ height: 60 }}
            navbar={{
                width: 300,
                breakpoint: 'sm',
                collapsed: { mobile: !opened },
            }}
            padding="md"
            withBorder
        >
            <AppShell.Header>
                <Burger
                    opened={opened}
                    onClick={toggle}
                    hiddenFrom="sm"
                    size="sm"
                />
                <Text className="h-full flex items-center px-5 gap-x-2">
                    <Image src={logo.src} alt="OpenAdapt" w={40} />
                    <Text>
                        OpenAdapt.AI
                    </Text>
                </Text>
            </AppShell.Header>

            <AppShell.Navbar>
                <Navbar />
            </AppShell.Navbar>

            <AppShell.Main>{children}</AppShell.Main>
        </AppShell>
    )
}
