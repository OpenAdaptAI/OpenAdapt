import { Button, Flex, Table } from "@mantine/core"
import { IconRefresh } from '@tabler/icons-react'
import React from "react"

type Props<T extends Record<string, any>> = {
    columns: {
        name: string,
        accessor: string | ((row: T) => React.ReactNode),
    }[];
    data: T[],
    refreshData: () => void,
    onClickRow: (row: T) => (event: React.MouseEvent<HTMLTableRowElement, MouseEvent>) => void,
}

export function SimpleTable<T extends Record<string, any>>({
    columns,
    data,
    refreshData,
    onClickRow,
}: Props<T>) {
  return (
    <Flex direction="column" mt={20} align="flex-end">
        <Button w="fit-content" leftSection={<IconRefresh />} variant="outline" onClick={refreshData}
         className="hover:bg-accent bg-primary/80 hover:text-zinc-100 text-zinc-200 hover:text-white"
        >
            Refresh
        </Button>
        <Table mt={20} className="bg-zinc-100 shadow-xl rounded-xl">
            <Table.Thead>
                <Table.Tr>
                    {columns.map(({name}) => (
                        <Table.Th key={name}>{name}</Table.Th>
                    ))}
                </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
                {data.map((row, rowIndex) => (
                    <Table.Tr key={rowIndex} className="hover:cursor-pointer hover:bg-gray-300" onClick={onClickRow(row)}>
                        {columns.map(({accessor}, accesorIndex) => (
                            <Table.Td key={accesorIndex} py={20}>
                                {typeof accessor === 'string' ? row[accessor] : accessor(row)}
                            </Table.Td>
                        ))}
                    </Table.Tr>
                ))}
            </Table.Tbody>
        </Table>
    </Flex>
  )
}
