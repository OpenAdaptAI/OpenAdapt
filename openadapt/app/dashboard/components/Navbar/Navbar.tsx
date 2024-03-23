'use client'

import { usePathname } from 'next/navigation'
import { routes } from '@/app/routes'
import { Stack } from '@mantine/core'
import Link from 'next/link'
import React from 'react'

export const Navbar = () => {
    const currentRoute = usePathname()
    return (
        <Stack>
            {routes.map((route) => (
                <Link
                    href={route.path}
                    key={route.path}
                    className={
                        (currentRoute === route.path ? 'bg-gray-200' : '') +
                        ' p-5 no-underline'
                    }
                >
                    {route.name}
                </Link>
            ))}
        </Stack>
    )
}
