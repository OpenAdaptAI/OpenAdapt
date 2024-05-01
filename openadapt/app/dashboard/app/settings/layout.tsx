import { Box, Flex } from "@mantine/core"
import { Navbar } from "./navbar"
import { SettingsHeader } from "./SettingsHeader"

const routes = [
    { name: 'API Keys', path: '/settings' },
    { name: 'Scrubbing', path: '/settings/scrubbing' },
    { name: 'Record & Replay', path: '/settings/record_and_replay' },
]

export default function RootLayout({
    children,
}: {
    children: React.ReactNode
}) {
    return (
        <Box>
            <SettingsHeader routes={routes} />
            <Flex columnGap={20} mt={20} className="border-2 border-gray-200 border-solid" p={20} mih="85vh">
                <Box flex={1}>
                    <Navbar routes={routes} />
                </Box>
                <Box flex={3}>
                    {children}
                </Box>
            </Flex>
        </Box>
    )
}
