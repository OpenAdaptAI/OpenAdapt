import { Box, Text } from '@mantine/core'
import { IconCalendarCheck } from '@tabler/icons-react'
import Link from 'next/link'
import React from 'react'

export const BookACall = () => {
  return (
    <Box mx="auto" mt={250} w="fit-content">
        <IconCalendarCheck size={100} className='block mx-auto mb-5' />
        <Text size='xl' className='text-center'>
            <Link href="https://www.getclockwise.com/c/richard-abrich/quick-meeting" className='no-underline'>
                Book a call with us
            </Link>
            <br />
            <Text component='span'> to discuss how OpenAdapt can help your team</Text>
        </Text>
    </Box>
  )
}
