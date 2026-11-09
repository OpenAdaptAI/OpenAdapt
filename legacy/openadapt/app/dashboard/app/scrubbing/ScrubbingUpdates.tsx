import { ScrubbingUpdate } from '@/types/scrubbing';
import { Box, Button, Container, Progress, Stack, Text } from '@mantine/core';
import React from 'react'

type Props = {
  data?: ScrubbingUpdate;
  resetScrubbingStatus: () => void;
}

export const ScrubbingUpdates = ({ data, resetScrubbingStatus }: Props) => {
  if (!data) {
    return null;
  }
  const isScrubbingComplete = (
    data.total_action_events > 0 &&
    data.total_window_events > 0 &&
    data.total_screenshots > 0 &&
    data.num_action_events_scrubbed === data.total_action_events &&
    data.num_window_events_scrubbed === data.total_window_events &&
    data.num_screenshots_scrubbed === data.total_screenshots
  ) || data.error;
  return (
    <Container size="sm">
      <Stack gap="xl">
        <Text component='h2'>Scrubbing updates for recording {data.recording.task_description}</Text>
        <Text>Provider: {data.provider}</Text>
        {data.copying_recording ? (
          <Text>Copying recording (this may take a while if Spacy dependencies need to be downloaded on the first run)...</Text>
        ) : data.error ? (
          <Text c="red">{data.error}</Text>
        ) : (
          <Stack gap="xl">
            <Box>
              <Progress value={data.num_action_events_scrubbed / data.total_action_events * 100} size="md" />
              <Text>{data.num_action_events_scrubbed} / {data.total_action_events} action events scrubbed</Text>
            </Box>
            <Box>
              <Progress value={data.num_window_events_scrubbed / data.total_window_events * 100} size="md" />
              <Text>{data.num_window_events_scrubbed} / {data.total_window_events} window events scrubbed</Text>
            </Box>
            <Box>
              <Progress value={data.num_screenshots_scrubbed / data.total_screenshots * 100} size="md" />
              <Text>{data.num_screenshots_scrubbed} / {data.total_screenshots} screenshots scrubbed</Text>
            </Box>
          </Stack>
        )}
        {isScrubbingComplete && (
            <Box>
              <Text component='h3'>Scrubbing complete!</Text>
              <Button onClick={resetScrubbingStatus} mt={10}>Scrub another recording</Button>
            </Box>
        )}
      </Stack>
    </Container>
  )
}
