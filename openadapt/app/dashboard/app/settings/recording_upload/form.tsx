'use client'

import { Button, Checkbox, Flex, Grid, PasswordInput, TextInput } from '@mantine/core'
import { useForm } from '@mantine/form'
import React, { useEffect } from 'react'
import {
    RecordingUploadSettings,
    saveSettings,
    validateRecordingUploadSettings,
} from '../utils'

type Props = {
    settings: RecordingUploadSettings
}

export const Form = ({ settings }: Props) => {
    const form = useForm({
        initialValues: JSON.parse(JSON.stringify(settings)),
        validate: (values) => {
            return validateRecordingUploadSettings(values)
        },
    })

    useEffect(() => {
        form.setValues(JSON.parse(JSON.stringify(settings)))
        form.setInitialValues(JSON.parse(JSON.stringify(settings)))
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [settings])

    function resetForm() {
        form.reset()
    }

    return (
        <form onSubmit={form.onSubmit(saveSettings(form))}>
            <Grid>
                <Grid.Col span={12}>
                    <Checkbox
                        label="Overwrite recording destination"
                        {...form.getInputProps(
                            'OVERWRITE_RECORDING_DESTINATION'
                        )}
                        checked={form.values.OVERWRITE_RECORDING_DESTINATION}
                    />
                </Grid.Col>
                <Grid.Col span={6}>
                    <PasswordInput
                        disabled={!form.values.OVERWRITE_RECORDING_DESTINATION}
                        label="Recording destination public key"
                        placeholder="Recording destination public key"
                        {...form.getInputProps('RECORDING_PUBLIC_KEY')}
                    />
                </Grid.Col>
                <Grid.Col span={6}>
                    <PasswordInput
                        disabled={!form.values.OVERWRITE_RECORDING_DESTINATION}
                        label="Recording destination private key"
                        placeholder="Recording destination private key"
                        {...form.getInputProps('RECORDING_PRIVATE_KEY')}
                    />
                </Grid.Col>
                <Grid.Col span={6}>
                    <TextInput
                        disabled={!form.values.OVERWRITE_RECORDING_DESTINATION}
                        label="Recording destination bucket name"
                        placeholder="Recording destination bucket name"
                        {...form.getInputProps('RECORDING_BUCKET_NAME')}
                    />
                </Grid.Col>
                <Grid.Col span={6}>
                    <TextInput
                        disabled={!form.values.OVERWRITE_RECORDING_DESTINATION}
                        label="Recording destination bucket region"
                        placeholder="Recording destination bucket region"
                        {...form.getInputProps('RECORDING_BUCKET_REGION')}
                    />
                </Grid.Col>
            </Grid>
            <Flex mt={40} columnGap={20}>
                <Button disabled={!form.isDirty()} type="submit">
                    Save settings
                </Button>
                <Button
                    variant="subtle"
                    disabled={!form.isDirty()}
                    onClick={resetForm}
                >
                    Discard changes
                </Button>
            </Flex>
        </form>
    )
}
