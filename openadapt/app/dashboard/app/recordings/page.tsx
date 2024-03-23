'use client';

import { SimpleTable } from "@/components/SimpleTable";
import { Recording, RecordingStatus } from "@/types/recording";
import { Box, Button } from "@mantine/core";
import { useEffect, useState } from "react";


export default function Recordings() {
    const [recordingStatus, setRecordingStatus] = useState(RecordingStatus.UNKNOWN);
    const [recordings, setRecordings] = useState<Recording[]>([])

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
        fetch('/api/recordings/stop').then(res => {
            if (res.ok) {
                setRecordingStatus(RecordingStatus.STOPPED);
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
                columnNames={['ID', 'Description', 'Start time', 'Timestamp', 'Monitor Width/Height', 'Double Click Interval Seconds/Pixels']}
                columnIdentifiers={[
                    'id',
                    'task_description',
                    (recording: Recording) => recording.video_start_time ? new Date(recording.video_start_time).toLocaleString() : '',
                    (recording: Recording) => new Date(recording.timestamp).toLocaleString(),
                    (recording: Recording) => `${recording.monitor_width}/${recording.monitor_height}`,
                    (recording: Recording) => `${recording.double_click_interval_seconds}/${recording.double_click_distance_pixels}`,
                ]}
                data={recordings}
                refreshData={fetchRecordings}
            />
        </Box>
    )
}
