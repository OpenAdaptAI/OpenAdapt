'use client';

import { ActionEvent as ActionEventType } from "@/types/action-event";
import { Accordion } from "@mantine/core";
import { useState } from "react";
import { ActionEvent } from "./ActionEvent";

type Props = {
    events: ActionEventType[]
}

export const ActionEvents = ({
    events
}: Props) => {
    const [values, setValues] = useState<string[]>([])
    return (
        <Accordion multiple value={values} onChange={setValues}>
            {events.map((event) => (
                <ActionEvent key={event.id} event={event} values={values} level={0} />
            ))}
        </Accordion>
    )
}
