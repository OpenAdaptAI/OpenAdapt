'use client'
import posthog from 'posthog-js'
import { PostHogProvider } from 'posthog-js/react'


const NEXT_PUBLIC_POSTHOG_KEY = ""
const NEXT_PUBLIC_POSTHOG_HOST = "https://us.i.posthog.com"

if (typeof window !== 'undefined') {
  posthog.init(NEXT_PUBLIC_POSTHOG_KEY, {
    api_host: NEXT_PUBLIC_POSTHOG_HOST,
  })
}

export function CSPostHogProvider({ children }: { children: React.ReactNode }) {
    return <PostHogProvider client={posthog}>{children}</PostHogProvider>
}
