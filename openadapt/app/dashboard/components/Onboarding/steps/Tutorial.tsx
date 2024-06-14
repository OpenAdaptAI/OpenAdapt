import { Box } from '@mantine/core'
import React from 'react'

export const Tutorial = () => {
  return (
    <Box>
      <video controls className='h-[80%] block mx-auto mt-20'>
        <source src="https://openadapt.ai/demo.mp4" type="video/mp4" />
      </video>
    </Box>
  )
}
