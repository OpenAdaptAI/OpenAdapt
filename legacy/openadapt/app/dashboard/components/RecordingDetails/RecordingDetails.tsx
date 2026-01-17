'use client';

import { timeStampToDateString } from '@/app/utils';
import { Recording } from '@/types/recording'
import { Table } from '@mantine/core'
import React from 'react'

type Props = {
    recording: Recording;
}

const TableRowWithBorder = ({ children }: { children: React.ReactNode }) => (
    <Table.Tr className='border-2 border-gray-300 border-solid'>
        {children}
    </Table.Tr>
)

const TableCellWithBorder = ({ children }: { children: React.ReactNode }) => (
    <Table.Td className='border-2 border-gray-300 border-solid'>
        {children}
    </Table.Td>
)

export const RecordingDetails = ({
    recording
}: Props) => {
  return (
        <Table withTableBorder withColumnBorders w={600}>
            <Table.Tbody>
                <TableRowWithBorder>
                    <TableCellWithBorder>Recording ID</TableCellWithBorder>
                    <TableCellWithBorder>{recording.id}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>timestamp</TableCellWithBorder>
                    <TableCellWithBorder>{timeStampToDateString(recording.timestamp)}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>monitor width</TableCellWithBorder>
                    <TableCellWithBorder>{recording.monitor_width}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>monitor height</TableCellWithBorder>
                    <TableCellWithBorder>{recording.monitor_height}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>double click interval seconds</TableCellWithBorder>
                    <TableCellWithBorder>{recording.double_click_interval_seconds}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>double click distance pixels</TableCellWithBorder>
                    <TableCellWithBorder>{recording.double_click_distance_pixels}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>platform</TableCellWithBorder>
                    <TableCellWithBorder>{recording.platform}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>task description</TableCellWithBorder>
                    <TableCellWithBorder>{recording.task_description}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>video start time</TableCellWithBorder>
                    <TableCellWithBorder>{recording.video_start_time}</TableCellWithBorder>
                </TableRowWithBorder>
            </Table.Tbody>
        </Table>
  )
}
