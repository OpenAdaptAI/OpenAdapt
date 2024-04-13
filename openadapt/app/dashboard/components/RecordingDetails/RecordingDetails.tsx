'use client';

import { timeStampToDateString } from '@/app/utils';
import { Recording } from '@/types/recording'
import { Table } from '@mantine/core'
import React from 'react'

type Props = {
    recording: Recording;
}

export const RecordingDetails = ({
    recording
}: Props) => {
  return (
        <Table withTableBorder withColumnBorders w={600} mb={50}>
            <Table.Tbody>
                <Table.Tr>
                    <Table.Td>Recording ID</Table.Td>
                    <Table.Td>{recording.id}</Table.Td>
                </Table.Tr>
                <Table.Tr>
                    <Table.Td>timestamp</Table.Td>
                    <Table.Td>{timeStampToDateString(recording.timestamp)}</Table.Td>
                </Table.Tr>
                <Table.Tr>
                    <Table.Td>monitor width</Table.Td>
                    <Table.Td>{recording.monitor_width}</Table.Td>
                </Table.Tr>
                <Table.Tr>
                    <Table.Td>monitor height</Table.Td>
                    <Table.Td>{recording.monitor_height}</Table.Td>
                </Table.Tr>
                <Table.Tr>
                    <Table.Td>double click interval seconds</Table.Td>
                    <Table.Td>{recording.double_click_interval_seconds}</Table.Td>
                </Table.Tr>
                <Table.Tr>
                    <Table.Td>double click distance pixels</Table.Td>
                    <Table.Td>{recording.double_click_distance_pixels}</Table.Td>
                </Table.Tr>
                <Table.Tr>
                    <Table.Td>platform</Table.Td>
                    <Table.Td>{recording.platform}</Table.Td>
                </Table.Tr>
                <Table.Tr>
                    <Table.Td>task description</Table.Td>
                    <Table.Td>{recording.task_description}</Table.Td>
                </Table.Tr>
                <Table.Tr>
                    <Table.Td>video start time</Table.Td>
                    <Table.Td>{recording.video_start_time}</Table.Td>
                </Table.Tr>
            </Table.Tbody>
        </Table>
  )
}
