'use client';

import { timeStampToDateString } from '@/app/utils';
import { Replay } from '@/types/replay'
import { Code, Table } from '@mantine/core'
import React from 'react'

type Props = {
    replay: Replay;
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

export const ReplayDetails = ({
    replay
}: Props) => {
  return (
        <Table withTableBorder withColumnBorders w={600}>
            <Table.Tbody>
                <TableRowWithBorder>
                    <TableCellWithBorder>Replay ID</TableCellWithBorder>
                    <TableCellWithBorder>{replay.id}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>timestamp</TableCellWithBorder>
                    <TableCellWithBorder>{timeStampToDateString(replay.timestamp)}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>strategy name</TableCellWithBorder>
                    <TableCellWithBorder>{replay.strategy_name}</TableCellWithBorder>
                </TableRowWithBorder>
                <TableRowWithBorder>
                    <TableCellWithBorder>strategy args</TableCellWithBorder>
                    <TableCellWithBorder><Code>{JSON.stringify(replay.strategy_args)}</Code></TableCellWithBorder>
                </TableRowWithBorder>
            </Table.Tbody>
        </Table>
  )
}
