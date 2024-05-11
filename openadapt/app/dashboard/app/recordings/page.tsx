'use client';

import { SimpleTable } from "@/components/SimpleTable";
import { Recording, RecordingStatus } from "@/types/recording";
import { Box, Button } from "@mantine/core";
import { useEffect, useState } from "react";
import { timeStampToDateString } from "../utils";
import { useRouter } from "next/navigation";


export default function Recordings() {
    const [recordingStatus, setRecordingStatus] = useState(RecordingStatus.UNKNOWN);
    const [recordings, setRecordings] = useState<Recording[]>([]);

    const router = useRouter();

    function startRecording() {
        if (recordingStatus === RecordingStatus.RECORDING) {
            return;
        }
        fetch('/api/recordings/start').then(res => {
            if (res.ok) {
                setRecordingStatus(RecordingStatus.RECORDING);
            }
        });
    }
    function stopRecording() {
        if (recordingStatus === RecordingStatus.STOPPED) {
            return;
        }
        setRecordingStatus(RecordingStatus.UNKNOWN);
        fetch('/api/recordings/stop').then(res => {
            if (res.ok) {
                setRecordingStatus(RecordingStatus.STOPPED);
                fetchRecordings();
            }
        });
    }
    function fetchRecordings() {
        fetch('/api/recordings').then(res => {
            if (res.ok) {
                res.json().then((data) => {
                    setRecordings(data.recordings);
                });
            }
        })
    }
    function fetchRecordingStatus() {
        fetch('/api/recordings/status').then(res => {
            if (res.ok) {
                res.json().then((data) => {
                    if (data.recording) {
                        setRecordingStatus(RecordingStatus.RECORDING);
                    } else {
                        setRecordingStatus(RecordingStatus.STOPPED);
                    }
                });
            }
        });
    }

    function goToRecording(recording: Recording) {
        return function() {
            router.push(`/recordings/detail?id=${recording.id}`);
        }
    }

    useEffect(() => {
        fetchRecordings();
        fetchRecordingStatus();
    }, []);

    return (
        <Box>
            {recordingStatus === RecordingStatus.RECORDING && (
                <Button onClick={stopRecording} variant="outline" color="red">
                    Stop recording
                </Button>
            )}
            {recordingStatus === RecordingStatus.STOPPED && (
                <Button onClick={startRecording} variant="outline" color="blue">
                    Start recording
                </Button>
            )}
            {recordingStatus === RecordingStatus.UNKNOWN && (
                <Button variant="outline" color="blue">
                    Loading recording status...
                </Button>
            )}
            <SimpleTable
                columns={[
                    {name: 'ID', accessor: 'id'},
                    {name: 'Description', accessor: 'task_description'},
                    {name: 'Start time', accessor: (recording: Recording) => recording.video_start_time ? timeStampToDateString(recording.video_start_time) : 'N/A'},
                    {name: 'Timestamp', accessor: (recording: Recording) => recording.timestamp ? timeStampToDateString(recording.timestamp) : 'N/A'},
                    {name: 'Monitor Width/Height', accessor: (recording: Recording) => `${recording.monitor_width}/${recording.monitor_height}`},
                    {name: 'Double Click Interval Seconds/Pixels', accessor: (recording: Recording) => `${recording.double_click_interval_seconds}/${recording.double_click_distance_pixels}`},
                ]}
                data={recordings}
                refreshData={fetchRecordings}
                onClickRow={goToRecording}
            />
        </Box>
    )
}
