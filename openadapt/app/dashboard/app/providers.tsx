'use client'
import { get } from '@/api'
import posthog from 'posthog-js'
import { PostHogProvider } from 'posthog-js/react'
import { useEffect } from 'react'
import { getSettings } from './utils'

if (typeof window !== 'undefined') {
    if (process.env.NEXT_PUBLIC_MODE !== 'development') {
        posthog.init(process.env.NEXT_PUBLIC_POSTHOG_PUBLIC_KEY as string, {
            api_host: process.env.NEXT_PUBLIC_POSTHOG_HOST,
        })
    }
}

export function CSPostHogProvider({ children }: { children: React.ReactNode }) {
    useEffect(() => {
        if (process.env.NEXT_PUBLIC_MODE !== 'development') {
            getSettings().then((settings) => {
                posthog.identify(settings['UNIQUE_USER_ID'])
            })
        }
    }, [])
    if (process.env.NEXT_PUBLIC_MODE === 'development') {
        return <>{children}</>
    }
    return <PostHogProvider client={posthog}>{children}</PostHogProvider>
}
