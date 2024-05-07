'use client';

import { timeStampToDateString } from '@/app/utils';
import { ActionEvent as ActionEventType } from '@/types/action-event'
import { Accordion, Box, Grid, Image, Table } from '@mantine/core'
import { useHover } from '@mantine/hooks';

type Props = {
    event: ActionEventType;
    values: string[];
    level: number;
    openScreenshotModal: () => void;
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

export const ActionEvent = ({
    event,
    values,
    level,
    openScreenshotModal: _openScreenshotModal,
}: Props) => {
    const {ref: screenshotRef, hovered: hoveredOverScreenshot } = useHover<HTMLImageElement>();
    const openScreenshotModal = (e: React.MouseEvent<HTMLImageElement>) => {
        e.stopPropagation();
        _openScreenshotModal();
    }
    const imageSrc = (hoveredOverScreenshot ? event.screenshot : event.screenshot) || ''; // change to event.diff to show diff

    let content = (
        <Grid>
            <Grid.Col span={level === 0 ? 4 : 12}>
                <Table w={400} withTableBorder withColumnBorders my={20} className='border-2 border-gray-300 border-solid'>
                    <Table.Tbody>
                        {typeof event.id === 'number' && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>id</TableCellWithBorder>
                                <TableCellWithBorder>{event.id}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        {event.name && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>name</TableCellWithBorder>
                                <TableCellWithBorder>{event.name}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        <TableRowWithBorder>
                            <TableCellWithBorder>timestamp</TableCellWithBorder>
                            <TableCellWithBorder>{timeStampToDateString(event.timestamp)}</TableCellWithBorder>
                        </TableRowWithBorder>
                        {event.screenshot_timestamp && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>screenshot timestamp</TableCellWithBorder>
                                <TableCellWithBorder>{timeStampToDateString(event.screenshot_timestamp)}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        <TableRowWithBorder>
                            <TableCellWithBorder>window event timestamp</TableCellWithBorder>
                            <TableCellWithBorder>{timeStampToDateString(event.window_event_timestamp)}</TableCellWithBorder>
                        </TableRowWithBorder>
                        {event.mouse_x && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>mouse x</TableCellWithBorder>
                                <TableCellWithBorder>{event.mouse_x}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        {event.mouse_y && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>mouse y</TableCellWithBorder>
                                <TableCellWithBorder>{event.mouse_y}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        {event.mouse_button_name && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>mouse button name</TableCellWithBorder>
                                <TableCellWithBorder>{event.mouse_button_name}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        {event.mouse_pressed && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>mouse pressed</TableCellWithBorder>
                                <TableCellWithBorder>{event.mouse_pressed.toString()}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        {event.text && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>text</TableCellWithBorder>
                                <TableCellWithBorder>{event.text}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        {event.canonical_text && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>canonical text</TableCellWithBorder>
                                <TableCellWithBorder>{event.canonical_text}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        {event.key_name && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>key name</TableCellWithBorder>
                                <TableCellWithBorder>{event.key_name}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        {event.canonical_key_vk && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>canonical key vk</TableCellWithBorder>
                                <TableCellWithBorder>{event.canonical_key_vk}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        {event.parent_id && (
                            <TableRowWithBorder>
                                <TableCellWithBorder>parent id</TableCellWithBorder>
                                <TableCellWithBorder>{event.parent_id}</TableCellWithBorder>
                            </TableRowWithBorder>
                        )}
                        <TableRowWithBorder>
                            <TableCellWithBorder>children</TableCellWithBorder>
                            <TableCellWithBorder>{event.children?.length || 0}</TableCellWithBorder>
                        </TableRowWithBorder>
                    </Table.Tbody>
                </Table>
            </Grid.Col>
            {level === 0 && (
                <Grid.Col span={12}>
                    {event.screenshot !== null && (
                        <Image onClick={openScreenshotModal} ref={screenshotRef} src={imageSrc} alt={`Screenshot of ${event.name}`} w="100%" h="auto" pb={40} mr="auto" />
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
                {displayChild && values.includes(event.id.toString()) && event.children && (
                    <Accordion.Panel ml={100}>
                        {event.children.map((child) => (
                            <Box key={child.id}>
                                <ActionEvent event={child} values={values} level={level + 1} openScreenshotModal={_openScreenshotModal} />
                            </Box>
                        ))}
                    </Accordion.Panel>
                )}
            </Accordion.Item>
        )
    }


    return (
        <Box className='border-2 border-gray-300 border-solid' my={10} px={10}>
            {content}
        </Box>
    )
}
