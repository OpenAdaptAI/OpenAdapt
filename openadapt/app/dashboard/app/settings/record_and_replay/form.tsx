'use client';

import { Button, Checkbox, Flex, Grid, TextInput } from '@mantine/core';
import { useForm } from '@mantine/form';
import React, { useEffect } from 'react'
import { saveSettings, validateRecordAndReplaySettings } from '../utils';

type Props = {
    settings: Record<string, string>,
}

export const Form = ({
    settings,
}: Props) => {
    const form = useForm({
        initialValues: JSON.parse(JSON.stringify(settings)),
        validate: (values) => {
            return validateRecordAndReplaySettings(values);
        },
    })

    useEffect(() => {
        form.setValues(JSON.parse(JSON.stringify(settings)));
        form.setInitialValues(JSON.parse(JSON.stringify(settings)));
    }, [settings]);

    function resetForm() {
        form.reset();
    }

    return (
        <form onSubmit={form.onSubmit(saveSettings(form))}>
            <Grid>
                <Grid.Col span={6}>
                    <Checkbox label="Record window data" {...form.getInputProps('RECORD_WINDOW_DATA')} checked={form.values.RECORD_WINDOW_DATA} />
                </Grid.Col>
                <Grid.Col span={6}>
                    <Checkbox label="Record active element state" {...form.getInputProps('RECORD_ACTIVE_ELEMENT_STATE')} checked={form.values.RECORD_ACTIVE_ELEMENT_STATE} />
                </Grid.Col>
                <Grid.Col span={6}>
                    <Checkbox label="Record video" {...form.getInputProps('RECORD_VIDEO')} checked={form.values.RECORD_VIDEO} />
                </Grid.Col>
                <Grid.Col span={6}>
                    <Checkbox label="Record images" {...form.getInputProps('RECORD_IMAGES')} checked={form.values.RECORD_IMAGES} />
                </Grid.Col>
                <Grid.Col span={6}>
                    <TextInput label="Video pixel format" placeholder="Video pixel format" {...form.getInputProps('VIDEO_PIXEL_FORMAT')} />
                </Grid.Col>
            </Grid>
            <Flex mt={40} columnGap={20}>
                <Button disabled={!form.isDirty()} type="submit">
                    Save settings
                </Button>
                <Button variant="subtle" disabled={!form.isDirty()} onClick={resetForm}>
                    Discard changes
                </Button>
            </Flex>
        </form>
    )
}
