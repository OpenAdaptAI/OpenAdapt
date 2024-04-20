import './globals.css'

import { ColorSchemeScript, MantineProvider } from '@mantine/core'
import { Notifications } from '@mantine/notifications';
import { Shell } from '@/components/Shell'

export const metadata = {
    title: 'OpenAdapt',
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
            <body>
                <MantineProvider>
                    <Notifications />
                    <Shell>{children}</Shell>
                </MantineProvider>
            </body>
        </html>
    )
}
