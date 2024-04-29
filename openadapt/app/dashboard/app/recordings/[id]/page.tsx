'use client';

import { get } from "@/api";
import { ActionEvents } from "@/components/ActionEvent/ActionEvents";
import { RecordingDetails } from "@/components/RecordingDetails";
import { ActionEvent as ActionEventType } from "@/types/action-event";
import { Recording as RecordingType } from "@/types/recording";
import { Box } from "@mantine/core";
import { useEffect, useState } from "react";

type Props = {
    id: string;
}


export default function Recording({
    params: { id },
}: {params: Props}) {
    const [recording, setRecording] = useState<{
        recording: RecordingType,
        action_events: ActionEventType[],
    } | null>(null);
    useEffect(() => {
        fetchRecordingInfo(id).then((data) => {
            setRecording(data);
        });
    }, [id]);
    if (!recording) {
        return null;
    }
    const actionEvents = addIdsToNullActionEvents(recording.action_events);

    return (
        <Box>
            <RecordingDetails recording={recording.recording} />
            <ActionEvents events={actionEvents} />
        </Box>
    )
}


async function fetchRecordingInfo(id: string): Promise<{
    recording: RecordingType,
    action_events: ActionEventType[],
}> {
    return get(`/api/recordings/${id}`, {
        // add a no-store cache policy to prevent nextjs
        // from creating a static page at build time
        cache: "no-store",
    }).then((res) => res.json());
}

function addIdsToNullActionEvents(actionEvents: ActionEventType[]): ActionEventType[] {
    return actionEvents.map((event) => {
        if (!event.id) {
            // this is usually the case, when new events like 'singleclick'
            // or 'doubleclick' are created while merging several events together,
            // but they are not saved in the database
            return {
                ...event,
                id: crypto.randomUUID(),
            };
        }
        return event;
    });
}
