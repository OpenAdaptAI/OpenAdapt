import { Box, Text } from '@mantine/core'
import Link from 'next/link'
import React from 'react'

export const BookACall = () => {
  return (
    <Box mx="auto" w="fit-content">
        <Text size='xl' className='text-center'>
            <Link href="https://www.getclockwise.com/c/richard-abrich/quick-meeting" className='no-underline'>
                Book a call with us
            </Link>
            <Text component='span'> to discuss how OpenAdapt can help your team</Text>
        </Text>
    </Box>
  )
}
