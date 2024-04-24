import { get } from "@/api";
import { ActionEvents } from "@/components/ActionEvent/ActionEvents";
import { RecordingDetails } from "@/components/RecordingDetails";
import { ActionEvent as ActionEventType } from "@/types/action-event";
import { Recording as RecordingType } from "@/types/recording";
import { Box } from "@mantine/core";

type Props = {
    id: string;
}


export default async function Recording({
    params: { id },
}: {params: Props}) {
    const recording = await fetchRecordingInfo(id);
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
