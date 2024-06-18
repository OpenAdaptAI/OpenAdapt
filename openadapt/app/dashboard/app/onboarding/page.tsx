import { BookACall } from "@/components/Onboarding/steps/BookACall";
import { RegisterForUpdates } from "@/components/Onboarding/steps/RegisterForUpdates";
import { Tutorial } from "@/components/Onboarding/steps/Tutorial";
import { Box, Divider } from "@mantine/core";

export default function Onboarding() {
    return (
        <Box w="fit-content" mx="auto">
            <Tutorial />
            <Divider my={20} w="40vw" mx="auto" />
            <RegisterForUpdates />
            <Divider my={20} w="40vw" mx="auto" />
            <BookACall />
        </Box>
    );
}
