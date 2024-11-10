'use client'

import { Recording } from '@/types/recording'
import { ScrubbingStatus, ScrubbingUpdate } from '@/types/scrubbing'
import { Box, Button, Container, Grid, Select, Text } from '@mantine/core'
import { isNotEmpty, useForm } from '@mantine/form'
import { notifications } from '@mantine/notifications'
import { useEffect, useState } from 'react'
import { ScrubbingUpdates } from './ScrubbingUpdates'
import Link from 'next/link'

export default function Recordings() {
    const [recordings, setRecordings] = useState<Recording[]>([])
    const [scrubbingProviders, setScrubbingProviders] = useState<
        Record<string, string>
    >({})
    const [scrubbingStatus, setScrubbingStatus] = useState<ScrubbingStatus>(
        ScrubbingStatus.UNKNOWN
    )
    const [scrubbingUpdate, setScrubbingUpdate] = useState<ScrubbingUpdate>()
    const scrubbingForm = useForm({
        initialValues: {
            recordingId: '',
            providerId: '',
        },
        validate: {
            recordingId: isNotEmpty('Recording is required'),
            providerId: isNotEmpty('Provider is required'),
        },
    })
    function fetchRecordings() {
        fetch('/api/recordings').then((res) => {
            if (res.ok) {
                res.json().then((data) => {
                    setRecordings(data.recordings)
                })
            }
        })
    }
    function fetchScrubbingProviders() {
        fetch('/api/scrubbing/providers').then((res) => {
            if (res.ok) {
                res.json().then((data) => {
                    setScrubbingProviders(data)
                })
            }
        })
    }
    function fetchScrubbingStatus() {
        fetch('/api/scrubbing/status').then((res) => {
            if (res.ok) {
                res.json().then((data) => {
                    if (data.status) {
                        setScrubbingStatus(ScrubbingStatus.SCRUBBING)
                        fetchScrubbingUpdates()
                    } else {
                        setScrubbingStatus(ScrubbingStatus.STOPPED)
                    }
                })
            }
        })
    }
    function scrubRecording(values: {
        recordingId: string
        providerId: string
    }) {
        setScrubbingStatus(ScrubbingStatus.UNKNOWN)
        const { recordingId, providerId } = values
        fetch(`/api/scrubbing/scrub/${recordingId}/${providerId}`, {
            method: 'POST',
        })
            .then((res) => res.json())
            .then((data) => {
                if (data.status === 'success') {
                    setScrubbingStatus(ScrubbingStatus.SCRUBBING)
                } else {
                    notifications.show({
                        title: 'Error while scrubbing recording',
                        message: data.message,
                        color: 'red',
                    })
                }
                fetchScrubbingStatus()
            })
    }
    async function fetchScrubbingUpdates() {
        fetch('/api/scrubbing/updates').then((res) => {
            const reader = res.body?.getReader()
            const textDecoder = new TextDecoder('utf-8')
            setScrubbingStatus(ScrubbingStatus.SCRUBBING)
            reader?.read().then(function processText({ done, value }) {
                if (done) {
                    return
                }
                const data = JSON.parse(textDecoder.decode(value))
                setScrubbingUpdate(data)
                reader?.read().then(processText)
            })
        })
    }

    useEffect(() => {
        fetchRecordings()
        fetchScrubbingProviders()
        fetchScrubbingStatus()
    }, [])

    function resetScrubbingStatus() {
        fetchScrubbingStatus()
    }

    return (
        <Container size="sm">
            {scrubbingStatus === ScrubbingStatus.UNKNOWN ? (
                <Text>Checking scrubbing status...</Text>
            ) : scrubbingStatus === ScrubbingStatus.SCRUBBING ? (
                <ScrubbingUpdates
                    data={scrubbingUpdate}
                    resetScrubbingStatus={resetScrubbingStatus}
                />
            ) : (
                <form onSubmit={scrubbingForm.onSubmit(scrubRecording)}>
                    <Grid>
                        <Grid.Col span={6}>
                            <Select
                                data={recordings.map((recording) => ({
                                    value: recording.id.toString(),
                                    label: recording.task_description,
                                }))}
                                size="sm"
                                label="Select recording to scrub"
                                {...scrubbingForm.getInputProps('recordingId')}
                            />
                        </Grid.Col>
                        <Grid.Col span={6}>
                            <Select
                                data={Object.entries(scrubbingProviders).map(
                                    ([id, name]) => ({
                                        value: id,
                                        label: name,
                                    })
                                )}
                                size="sm"
                                label="Select scrubbing provider"
                                {...scrubbingForm.getInputProps('providerId')}
                            />
                        </Grid.Col>
                        <Grid.Col span={12}>
                            <Link
                                href="/settings/scrubbing"
                                className="no-underline"
                            >
                                Configure scrubbing settings before scrubbing
                            </Link>
                        </Grid.Col>
                    </Grid>
                    <Button mt={20} type="submit">
                        Scrub
                    </Button>
                </form>
            )}
        </Container>
    )
}
