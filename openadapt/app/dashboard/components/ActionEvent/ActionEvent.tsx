'use client';

import { timeStampToDateString } from '@/app/utils';
import { ActionEvent as ActionEventType } from '@/types/action-event'
import { Accordion, Box, Grid, Table } from '@mantine/core'
import Image from 'next/image';

type Props = {
    event: ActionEventType,
    values: string[],
    level: number,
}

export const ActionEvent = ({
    event,
    values,
    level,
}: Props) => {
    let content = (
        <Grid>
            <Grid.Col span={level === 0 ? 4 : 12}>
                <Table w={400} withTableBorder withColumnBorders my={20}>
                    <Table.Tbody>
                        {typeof event.id === 'number' && (
                            <Table.Tr>
                                <Table.Td>id</Table.Td>
                                <Table.Td>{event.id}</Table.Td>
                            </Table.Tr>
                        )}
                        {event.name && (
                            <Table.Tr>
                                <Table.Td>name</Table.Td>
                                <Table.Td>{event.name}</Table.Td>
                            </Table.Tr>
                        )}
                        <Table.Tr>
                            <Table.Td>timestamp</Table.Td>
                            <Table.Td>{timeStampToDateString(event.timestamp)}</Table.Td>
                        </Table.Tr>
                        {event.screenshot_timestamp && (
                            <Table.Tr>
                                <Table.Td>screenshot timestamp</Table.Td>
                                <Table.Td>{timeStampToDateString(event.screenshot_timestamp)}</Table.Td>
                            </Table.Tr>
                        )}
                        <Table.Tr>
                            <Table.Td>window event timestamp</Table.Td>
                            <Table.Td>{timeStampToDateString(event.window_event_timestamp)}</Table.Td>
                        </Table.Tr>
                        {event.mouse_x && (
                            <Table.Tr>
                                <Table.Td>mouse x</Table.Td>
                                <Table.Td>{event.mouse_x}</Table.Td>
                            </Table.Tr>
                        )}
                        {event.mouse_y && (
                            <Table.Tr>
                                <Table.Td>mouse y</Table.Td>
                                <Table.Td>{event.mouse_y}</Table.Td>
                            </Table.Tr>
                        )}
                        {event.mouse_button_name && (
                            <Table.Tr>
                                <Table.Td>mouse button name</Table.Td>
                                <Table.Td>{event.mouse_button_name}</Table.Td>
                            </Table.Tr>
                        )}
                        {event.mouse_pressed && (
                            <Table.Tr>
                                <Table.Td>mouse pressed</Table.Td>
                                <Table.Td>{event.mouse_pressed.toString()}</Table.Td>
                            </Table.Tr>
                        )}
                        {event.text && (
                            <Table.Tr>
                                <Table.Td>text</Table.Td>
                                <Table.Td>{event.text}</Table.Td>
                            </Table.Tr>
                        )}
                        {event.canonical_text && (
                            <Table.Tr>
                                <Table.Td>canonical text</Table.Td>
                                <Table.Td>{event.canonical_text}</Table.Td>
                            </Table.Tr>
                        )}
                        {event.key_name && (
                            <Table.Tr>
                                <Table.Td>key name</Table.Td>
                                <Table.Td>{event.key_name}</Table.Td>
                            </Table.Tr>
                        )}
                        {event.canonical_key_vk && (
                            <Table.Tr>
                                <Table.Td>canonical key vk</Table.Td>
                                <Table.Td>{event.canonical_key_vk}</Table.Td>
                            </Table.Tr>
                        )}
                        {event.parent_id && (
                            <Table.Tr>
                                <Table.Td>parent id</Table.Td>
                                <Table.Td>{event.parent_id}</Table.Td>
                            </Table.Tr>
                        )}
                        <Table.Tr>
                            <Table.Td>children</Table.Td>
                            <Table.Td>{event.children?.length || 0}</Table.Td>
                        </Table.Tr>
                    </Table.Tbody>
                </Table>
            </Grid.Col>
            {level === 0 && (
                <Grid.Col span={8}>
                    {event.screenshot !== null && (
                        <Image src={event.screenshot} alt={`Screenshot of ${event.name}`} width={800} height={600} className='py-4' />
                    )}
                </Grid.Col>
            )}
        </Grid>
    )

    const displayChild = event.children && event.children.length > 0 && event.children[0].id;

    if (displayChild) {
        content = (
            <Accordion.Item value={event.id.toString()}>
                <Accordion.Control p={0} classNames={{
                    label: 'p-0',
                }}>
                    {content}
                </Accordion.Control>
                {displayChild && values.includes(event.id.toString()) && (
                    <Accordion.Panel ml={(level + 1) * 100}>
                        {/* @ts-ignore */}
                        {event.children.map((child) => (
                            <Box key={child.id}>
                                <ActionEvent event={child} values={values} level={level + 1} />
                            </Box>
                        ))}
                    </Accordion.Panel>
                )}
            </Accordion.Item>
        )
    }


    return (
        <Box className='border-2 border-black border-solid' my={10} px={10}>
            {content}
        </Box>
    )
}
