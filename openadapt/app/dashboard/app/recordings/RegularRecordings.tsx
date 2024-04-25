import { SimpleTable } from '@/components/SimpleTable';
import { Recording } from '@/types/recording';
import React, { useEffect, useState } from 'react'
import { timeStampToDateString } from '../utils';
import { useRouter } from 'next/navigation';

export const RegularRecordings = () => {
    const [recordings, setRecordings] = useState<Recording[]>([]);
    const router = useRouter();

    function fetchRecordings() {
        fetch('/api/recordings').then(res => {
            if (res.ok) {
                res.json().then((data) => {
                    setRecordings(data.recordings);
                });
            }
        })
    }

    useEffect(() => {
        fetchRecordings();
    }, []);

    function onClickRow(recording: Recording) {
        return () => router.push(`/recordings/detail/?id=${recording.id}`);
    }

    return (
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
            onClickRow={onClickRow}
        />
    )
}
