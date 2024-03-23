import { Box, Button, Flex, Table } from "@mantine/core"
import { IconRefresh } from '@tabler/icons-react'

type Props<T extends Record<string, any>> = {
    columnNames: string[],
    // columnIdentifiers, which is an array, which can contain strings, or a function that takes an object and returns a string
    columnIdentifiers: (string | ((row: T) => string))[],
    data: T[],
    refreshData: () => void,
}

export function SimpleTable<T extends Record<string, any>>({
    columnNames,
    columnIdentifiers,
    data,
    refreshData,
}: Props<T>) {
  return (
    <Flex direction="column" mt={20} align="flex-end">
        <Button w="fit-content" leftSection={<IconRefresh />} variant="outline" onClick={refreshData}>
            Refresh
        </Button>
        <Table mt={20}>
            <Table.Thead>
                <Table.Tr>
                    {columnNames.map((name) => (
                        <Table.Th key={name}>{name}</Table.Th>
                    ))}
                </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
                {data.map((row, rowIndex) => (
                    <Table.Tr key={rowIndex}>
                        {columnIdentifiers.map((identifier, identifierIndex) => (
                            <Table.Td key={identifierIndex}>
                                {typeof identifier === 'string' ? row[identifier] : identifier(row)}
                            </Table.Td>
                        ))}
                    </Table.Tr>
                ))}
            </Table.Tbody>
        </Table>
    </Flex>
  )
}
