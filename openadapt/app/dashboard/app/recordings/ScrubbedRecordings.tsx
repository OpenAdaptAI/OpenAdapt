import { SimpleTable } from '@/components/SimpleTable'
import { Recording, ScrubbedRecording } from '@/types/recording'
import { useRouter } from 'next/navigation'
import React, { useEffect, useState } from 'react'

export const ScrubbedRecordings = () => {
    const [recordings, setRecordings] = useState<ScrubbedRecording[]>([])
    const router = useRouter()

    function fetchScrubbedRecordings() {
        fetch('/api/recordings/scrubbed').then((res) => {
            if (res.ok) {
                res.json().then((data) => {
                    setRecordings(data.recordings)
                })
            }
        })
    }

    function onClickRow(recording: ScrubbedRecording) {
        return () =>
            router.push(`/recordings/detail/?id=${recording.recording_id}`)
    }

    useEffect(() => {
        fetchScrubbedRecordings()
    }, [])

    return (
        <SimpleTable
            columns={[
                {
                    name: 'ID',
                    accessor: (recording: ScrubbedRecording) =>
                        recording.recording_id,
                },
                {
                    name: 'Description',
                    accessor: (recording: ScrubbedRecording) =>
                        recording.recording.task_description,
                },
                { name: 'Provider', accessor: 'provider' },
                {
                    name: 'Original Recording',
                    accessor: (recording: ScrubbedRecording) =>
                        recording.original_recording.task_description,
                },
            ]}
            data={recordings}
            refreshData={fetchScrubbedRecordings}
            onClickRow={onClickRow}
        />
    )
}
