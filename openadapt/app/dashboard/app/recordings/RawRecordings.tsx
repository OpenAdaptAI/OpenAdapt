import { SimpleTable } from '@/components/SimpleTable'
import { Recording, UploadStatus } from '@/types/recording'
import React, { useEffect, useState } from 'react'
import { timeStampToDateString } from '../utils'
import { useRouter } from 'next/navigation'
import { Anchor, Button, Group, Text, Tooltip } from '@mantine/core'
import { IconInfoCircle } from '@tabler/icons-react'

export const RawRecordings = () => {
    const [recordings, setRecordings] = useState<Recording[]>([])
    const router = useRouter()

    function fetchRecordings() {
        fetch('/api/recordings').then((res) => {
            if (res.ok) {
                res.json().then((data) => {
                    setRecordings(data.recordings)
                })
            }
        })
    }

    useEffect(() => {
        fetchRecordings()
    }, [])

    function onClickRow(recording: Recording) {
        return () => router.push(`/recordings/detail/?id=${recording.id}`)
    }
    function goToSettings() {
        router.push('/settings/recording_upload')
    }

    function uploadRecording(
        e: React.MouseEvent<HTMLButtonElement>,
        recording_id: number
    ) {
        e.stopPropagation()
        fetch(`/api/recordings/${recording_id}/upload`, {
            method: 'POST',
        }).then((res) => {
            if (res.ok) {
                fetchRecordings()
            }
        })
    }

    return (
        <SimpleTable
            columns={[
                { name: 'ID', accessor: 'id' },
                { name: 'Description', accessor: 'task_description' },
                {
                    name: 'Start time',
                    accessor: (recording: Recording) =>
                        recording.video_start_time
                            ? timeStampToDateString(recording.video_start_time)
                            : 'N/A',
                },
                {
                    name: 'Timestamp',
                    accessor: (recording: Recording) =>
                        recording.timestamp
                            ? timeStampToDateString(recording.timestamp)
                            : 'N/A',
                },
                {
                    name: 'Monitor Width/Height',
                    accessor: (recording: Recording) =>
                        `${recording.monitor_width}/${recording.monitor_height}`,
                },
                {
                    name: 'Double Click Interval Seconds/Pixels',
                    accessor: (recording: Recording) =>
                        `${recording.double_click_interval_seconds}/${recording.double_click_distance_pixels}`,
                },
                {
                    name: (
                        <Group align="center">
                            <Text fw="bold">Upload to cloud</Text>
                            <Tooltip label="Edit settings to upload the recording to the cloud">
                                <Anchor c="black" display="flex" onClick={goToSettings}>
                                    <IconInfoCircle />
                                </Anchor>
                            </Tooltip>
                        </Group>
                    ),
                    accessor: (recording: Recording) =>
                        recording.upload_status === UploadStatus.UPLOADED ? (
                            <Anchor
                                onClick={(e) => e.stopPropagation()}
                                href={`/api/recordings/${recording.id}/view`}
                                target="_blank"
                            >
                                View
                            </Anchor>
                        ) : UploadStatus.UPLOADING ===
                          recording.upload_status ? (
                            'Uploading...'
                        ) : (
                            <Button
                                variant='outline'
                                onClick={(e) =>
                                    uploadRecording(e, recording.id)
                                }
                            >
                                Upload
                            </Button>
                        ),
                },
            ]}
            data={recordings}
            refreshData={fetchRecordings}
            onClickRow={onClickRow}
        />
    )
}
