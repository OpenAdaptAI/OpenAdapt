import './globals.css'

import { ColorSchemeScript, MantineProvider } from '@mantine/core'
import { Notifications } from '@mantine/notifications';
import { Shell } from '@/components/Shell'
import { CSPostHogProvider } from './providers';

export const metadata = {
    title: 'OpenAdapt.AI',
}

export default function RootLayout({
    children,
}: {
    children: React.ReactNode
}) {
    return (
        <html lang="en">
            <head>
                <ColorSchemeScript />
            </head>
            <CSPostHogProvider>
                <body>
                    <MantineProvider>
                        <Notifications />
                        <Shell>{children}</Shell>
                    </MantineProvider>
                </body>
            </CSPostHogProvider>
        </html>
    )
}
