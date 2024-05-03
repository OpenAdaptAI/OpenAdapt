'use client';

import { ActionEvent as ActionEventType } from "@/types/action-event";
import { Accordion } from "@mantine/core";
import { useState } from "react";
import { ActionEvent } from "./ActionEvent";
import { IconChevronDown } from "@tabler/icons-react";
import { Screenshots } from "./Screenshots";

type Props = {
    events: ActionEventType[]
}

export const ActionEvents = ({
    events
}: Props) => {
    const [values, setValues] = useState<string[]>([]);
    const [isScreenshotModalOpen, setIsScreenshotModalOpen] = useState(false);
    const toggleScreenshotModal = () => setIsScreenshotModalOpen(prev => !prev);
    return (
        <>
            <Accordion multiple value={values} onChange={setValues} chevronPosition="left" chevronSize={24} chevron={<IconChevronDown size={24} />}>
                {events.map((event) => (
                    <ActionEvent key={event.id} event={event} values={values} level={0} openScreenshotModal={toggleScreenshotModal} />
                ))}
            </Accordion>
            <Screenshots events={events} isOpen={isScreenshotModalOpen} onClose={toggleScreenshotModal} />
        </>
    )
}
