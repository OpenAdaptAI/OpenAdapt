type Route = {
    name: string
    path: string
}

export const routes: Route[] = [
    {
        name: 'Recordings',
        path: '/recordings',
    },
    {
        name: 'Settings',
        path: '/settings',
    },
    {
        name: 'Scrubbing',
        path: '/scrubbing',
    },
    {
        name: 'Onboarding',
        path: '/onboarding',
    },
    {
        name: "Replay logs",
        path: "/replay_logs",
    }
]
