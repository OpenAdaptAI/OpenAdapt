'use client';

import { SimpleTable } from "@/components/SimpleTable";
import { Replay } from "@/types/replay";
import { useEffect, useState } from "react";
import { timeStampToDateString } from "../utils";
import { Box, Text } from "@mantine/core";
import { useRouter } from "next/navigation";

export default function Replays() {
    const [replays, setReplays] = useState<Replay[]>([]);
    const fetchReplays = () => {
        fetch('/api/replays').then(res => {
            if (res.ok) {
                res.json().then((data) => {
                    setReplays(data);
                });
            }
        });
    }
    const router = useRouter();

    function onClickRow(replay: Replay) {
        return () => router.push(`/replays/logs?id=${replay.id}`);
    }

    useEffect(() => {
        fetchReplays();
    }, []);

    return (
        <Box>
            <Text size="xl">Replay logs</Text>
            <SimpleTable
                columns={[
                    {name: 'ID', accessor: 'id'},
                    {name: 'Strategy name', accessor: 'strategy_name'},
                    {name: 'Strategy arguments', accessor: (replay) => JSON.stringify(replay.strategy_args)},
                    {name: 'Time', accessor: (replay) => timeStampToDateString(replay.timestamp)},
                ]}
                data={replays}
                refreshData={fetchReplays}
                onClickRow={onClickRow}
            />
        </Box>
    )
}
