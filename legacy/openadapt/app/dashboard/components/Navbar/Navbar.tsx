'use client'

import { usePathname } from 'next/navigation'
import { routes } from '@/app/routes'
import { Stack } from '@mantine/core'
import Link from 'next/link'
import React from 'react'
import { IconChevronRight } from '@tabler/icons-react'

export const Navbar = () => {
    const currentRoute = usePathname()
    return (
        <Stack gap={0}>
            {routes.map((route) => (
                <Link
                    href={route.path}
                    key={route.path}
                    className={
                        (currentRoute.includes(route.path) ? 'bg-gray-200' : '') +
                        ' p-5 no-underline flex items-center gap-x-1 transition hover:gap-x-2 ease-out border-0 border-b-2 border-gray-100 border-solid'
                    }
                >
                    {route.name} <IconChevronRight size={20} />
                </Link>
            ))}
        </Stack>
    )
}
