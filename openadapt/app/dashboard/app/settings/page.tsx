import { get } from "@/api";
import { SettingsForm } from "@/components/SettingsForm";
import { Box, Text } from "@mantine/core";


async function getSettings(): Promise<Record<string, string>> {
    return get('/api/settings', {
        cache: 'no-store',
    })
}

export default async function Settings() {
    const settings = await getSettings();
    return (
        <Box>
            <Text component="h1" fz={24} mb={20}>Settings</Text>
            <SettingsForm settings={settings} />
        </Box>
    )
}
