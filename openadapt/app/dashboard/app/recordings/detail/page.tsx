'use client';

import { ActionEvents } from "@/components/ActionEvent/ActionEvents";
import { RecordingDetails } from "@/components/RecordingDetails";
import { ActionEvent as ActionEventType } from "@/types/action-event";
import { Recording as RecordingType } from "@/types/recording";
import { Box, Loader } from "@mantine/core";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

export default function Recording() {
    const searchParams = useSearchParams();
    const id = searchParams.get("id");
    const [recordingInfo, setRecordingInfo] = useState<{
        recording: RecordingType,
        action_events: ActionEventType[],
    }>();
    useEffect(() => {
        if (!id) {
            return;
        }
        const websocket = new WebSocket(`ws://${window.location.host}/api/recordings/${id}`);
        websocket.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === "recording") {
                setRecordingInfo(prev => {
                    if (!prev) {
                        return {
                            "recording": data.value,
                            "action_events": [],
                        }
                    }
                    return prev;
                });
            } else if (data.type === "action_event") {
                setRecordingInfo(prev => {
                    if (!prev) return prev;
                    return {
                        ...prev,
                        "action_events": [...prev.action_events, addIdToNullActionEvent(data.value)],
                    }
                });
            }
        }

        return () => {
            websocket.close();
        }
    }, [id]);
    if (!recordingInfo) {
        return <Loader />;
    }
    const actionEvents = recordingInfo.action_events;

    return (
        <Box>
            <RecordingDetails recording={recordingInfo.recording} />
            <ActionEvents events={actionEvents} />
        </Box>
    )
}

function addIdToNullActionEvent(actionEvent: ActionEventType): ActionEventType {
    let children = actionEvent.children;
    if (actionEvent.children) {
        children = actionEvent.children.map(addIdToNullActionEvent);
    }
    let id = actionEvent.id;
    if (!id) {
        // this is usually the case, when new events like 'singleclick'
        // or 'doubleclick' are created while merging several events together,
        // but they are not saved in the database
        id = crypto.randomUUID();
    }
    return {
        ...actionEvent,
        id,
        children,
    }
}
