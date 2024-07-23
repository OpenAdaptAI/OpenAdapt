'use client';

import { ReplayDetails } from "@/components/ReplayDetails";
import { ReplayLogs } from "@/components/ReplayLogs";
import { ReplayLog, Replay as ReplayType } from "@/types/replay";
import { Box, Loader, Progress } from "@mantine/core";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

function Replay() {
    const searchParams = useSearchParams();
    const id = searchParams.get("id");
    const [replayInfo, setReplayInfo] = useState<{
        replay: ReplayType,
        logs: ReplayLog[],
        num_logs: number,
    }>();
    useEffect(() => {
        if (!id) {
            return;
        }
        const websocket = new WebSocket(`ws://${window.location.host}/api/replays/${id}/logs`);
        websocket.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === "replay") {
                setReplayInfo(prev => {
                    if (!prev) {
                        return {
                            "replay": data.value,
                            "logs": [],
                            "num_logs": 0,
                        }
                    }
                    return prev;
                });
            } else if (data.type === "log") {
                setReplayInfo(prev => {
                    if (!prev) return prev;
                    return {
                        ...prev,
                        "logs": [...prev.logs, data.value],
                    }
                });
            } else if (data.type === "num_logs") {
                setReplayInfo(prev => {
                    if (!prev) return prev;
                    return {
                        ...prev,
                        "num_logs": data.value,
                    }
                });
            }
        }

        return () => {
            websocket.close();
        }
    }, [id]);
    if (!replayInfo) {
        return <Loader />;
    }

    const logs = replayInfo.logs;

    return (
        <Box>
            <ReplayDetails replay={replayInfo.replay} />
            {logs.length && logs.length < replayInfo.num_logs && (
                <Progress.Root size={30} my={30}>
                    <Progress.Section value={(logs.length / replayInfo.num_logs) * 100}>
                        <Progress.Label>Loading events {logs.length}/{replayInfo.num_logs}</Progress.Label>
                    </Progress.Section>
                </Progress.Root>
            )}
            <ReplayLogs logs={logs} />
        </Box>
    )
}


export default function ReplayPage() {
    return (
        <Suspense>
            <Replay />
        </Suspense>
    )
}
