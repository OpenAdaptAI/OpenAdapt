import { SimpleTable } from '@/components/SimpleTable'
import { Recording, UploadStatus } from '@/types/recording'
import React, { useEffect, useState } from 'react'
import { timeStampToDateString } from '../utils'
import { useRouter } from 'next/navigation'
import { Anchor, Button, Group, Text, Tooltip } from '@mantine/core'
import { IconInfoCircle } from '@tabler/icons-react'
import { modals } from '@mantine/modals'
import { getRecordingUploadSettings } from '../settings/utils'

export const RawRecordings = () => {
    const [recordings, setRecordings] = useState<Recording[]>([])
    const router = useRouter()
    const [settings, setSettings] = useState<
        Awaited<ReturnType<typeof getRecordingUploadSettings>>
    >({
        OVERWRITE_RECORDING_DESTINATION: false,
        RECORDING_PUBLIC_KEY: '',
        RECORDING_PRIVATE_KEY: '',
        RECORDING_BUCKET_NAME: '',
        RECORDING_BUCKET_REGION: '',
        RECORDING_DELETION_ENABLED: false,
    })
    useEffect(() => {
        getRecordingUploadSettings().then(setSettings)
    }, [])

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
        fetch(`/api/recordings/cloud/${recording_id}/upload`, {
            method: 'POST',
        }).then((res) => {
            if (res.ok) {
                fetchRecordings()
            }
        })
    }
    function deleteUploadedRecording(
        e: React.MouseEvent<HTMLAnchorElement, MouseEvent>,
        recording_id: number
    ) {
        e.stopPropagation();
        modals.openConfirmModal({
            title: 'Confirm deletion',
            children: (
                <Text size="sm">
                    Are you sure you want to delete the recording from cloud?
                </Text>
            ),
            labels: { confirm: 'Delete', cancel: 'Cancel' },
            confirmProps: {
                color: 'red'
            },
            onConfirm: () => {
                fetch(`/api/recordings/cloud/${recording_id}/delete`, {
                    method: 'POST',
                }).then((res) => {
                    if (res.ok) {
                        fetchRecordings()
                    }
                })
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
                            <Group>
                                <Anchor
                                    onClick={(e) => e.stopPropagation()}
                                    href={`/api/recordings/cloud/${recording.id}/view`}
                                    target="_blank"
                                >
                                    View
                                </Anchor>
                                {settings.RECORDING_DELETION_ENABLED && (
                                    <Anchor
                                        onClick={e => deleteUploadedRecording(e, recording.id)}
                                    target="_blank"
                                    c="red"
                                >
                                    Delete
                                    </Anchor>
                                )}
                            </Group>
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
