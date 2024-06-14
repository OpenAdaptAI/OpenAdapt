import { Button, Text } from '@mantine/core'
import React from 'react'

type Props = {
  completeOnboarding: () => void
}

export const CompleteOnboarding = ({
  completeOnboarding
}: Props) => {
  return (
    <Text mt={300} mx="auto" w={600}>
      Thanks for completing the onboarding process! You can always access these steps from the onboarding section in the sidebar.
      <Button onClick={completeOnboarding} mt={20} mx="auto" display="block">Finish onboarding</Button>
    </Text>
  )
}
